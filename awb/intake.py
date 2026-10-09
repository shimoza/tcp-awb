"""The intake: the only way customer material enters the Workbench.

`run` takes files from the vault inbox (or anywhere), reads each one with `awb.extract`, finds registered
forms and structured data in everything detection can see and writes one sanitised Markdown copy per file to
`<shared>/outbox/<CUST>/<file id>.md` (`<shared>/outbox/<tcp-xxxx>/` for a project without a customer): the
registered forms become their codes, structured data its tokens and every string the candidate rules of
`awb.wipe` recognise as a name a token of its class ([person 1], [company 1], [place 1], [name 1]), numbered per run; the forms of every wiped person and company are learned and wiped wherever
they stand again in the run (wipe mode, T4). Nothing stops for a name: `run(..., review=True)` is the one stop, for
the rare case where a name must stay distinguishable by a code. Every output is checked once more before it
counts. Originals move to
`<vault>/originals/<CUST>/` under their file id, only after that check passed. The public report goes next to
the outputs and carries codes, classes and counts. The private report goes to the vault and carries the rest.

When the vault is encrypted (register.tsv.gpg), the register is read through the vault daemon and every
original and private report of the run is sealed right after it is written (`seal_file` of the daemon writes
`<file>.gpg` and removes the plaintext). A file that cannot be sealed because no vault daemon answers stays in
plaintext inside the vault and is counted in `IntakeResult.unsealed`. When the daemon answers and refuses the seal
(locked, busy with a reload, refused), the run is undone: its originals go back where they came from, its outputs,
pictures and reports are removed and the run fails, so nothing of it stays in the vault in plaintext (D-T2h).

The original file name is treated as text: it is scanned and never used for an output. Nothing in this
module writes a matched value, a form or an original name into an exception, a log line or the public side.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from awb import codes, config, normalize, patterns, register, wipe
from awb import extract as _extract
from awb import images as _images
from awb import report as _report
from awb.extract.text import encoded_texts, expand_base64_blocks
from awb.matcher import Matcher, Span

FILE_ID_RE = re.compile(r"F-[A-Z2-7]{4}")
"""The shape of a file id. A member or attachment adds .1, .2 ... to the id of its file."""

MAX_PASSES = 4
"""How often the sanitiser normalises and replaces before it gives up and leaves the rest to the final check."""
SECOND_CLASS = "possible-name"
"""The class of a hit of the second check: a registered form found by its letters across word boundaries."""
_SKELETON_WORD_RE = wipe._SKELETON_WORD_RE
_SKELETON_MIN = 3
_SKELETON_CASE_FREE = 6
"""A single word shorter than this counts only when it does not stand in lower case: short forms that are also
ordinary words stay with the case rules of the matcher."""


_fold = wipe._fold
skeleton = wipe.skeleton

NEW = "new"


class IntakeError(Exception):
    """The intake cannot run. The message carries codes, counts and positions, never a name or a value."""


@dataclass
class IntakeResult:
    customer: str
    outputs: list[Path]
    public_report: Path
    private_report: Path
    states: dict[str, str]    # file id -> state
    unsealed: int = 0         # vault files of this run left in plaintext because sealing failed (encrypted vault)
    wiped: Counter = field(default_factory=Counter)   # token class (person, company, place, name) -> replacements
    held: list[str] = field(default_factory=list)     # file ids the final check withheld; their originals stay
    pictures: int = 0         # pictures held on the vault side, released only by awb images release
    reviewed: int = 0         # candidates handed to the review of --review
    candidates: int = 0       # distinct values the candidate rules found in detection (all of them wiped)

    @property
    def blocked(self) -> bool:
        """Always False: wipe mode never blocks (the callers and tests of the first release still ask)."""
        return False


# --------------------------------------------------------------------------- candidates

# the candidate rules live in awb/wipe.py; these names are the interface the rest of the Workbench uses
STRONG_RULES = wipe.STRONG_RULES
"""The rules that rarely fire on ordinary words: a short text such as a project goal is checked with these."""
SHORT_TEXT_RULES = STRONG_RULES + (wipe._first_name_pair_candidates,)
"""The rules of a commit message and a ledger line (TM0): the strong rules and a run that opens with a listed first
name. A technical term they take for a name goes into the section intake-seed of rules/allowed-terms.txt."""
_RULE_CLASS = {wipe._company_candidates: "company", wipe._label_candidates: "unknown",
               wipe._spaced_candidates: "unknown"}
keep_key = wipe.keep_key
WipeSpan = wipe.WipeSpan


def _collect(text: str, known: list[Span], state: wipe.WipeState | None = None) -> list[WipeSpan]:
    """The candidates of `text` that `known` does not cover: spans with their class (person, company, place,
    unknown) and the rule that found them (DESIGN.md section 2 of presentations/names)."""
    return wipe.collect(text, known, state)


def unknown_candidates(text: str, known: list[Span], rules=wipe._ALL_RULES, keep=()) -> list[str]:
    """The strings of `text` the rules of the first release take for names (wipe._ALL_RULES, or `rules`: the goal
    check of `awb spawn` passes STRONG_RULES), ordered by position, without duplicates; a phrase of `keep` (the keep
    list of the vault) is never one of them. The rules of wipe mode are `_collect`; `rules=None` gives its values.
    Private: the values go to the private report only."""
    if rules is None:
        return wipe.wipe_values(text, known, keep)
    return wipe.unknown_candidates(text, known, rules, keep)


_SHORT_ALLOWED = wipe._list("allowed-terms.txt", section="intake-seed")
"""The way out of SHORT_TEXT_RULES: a term of the section intake-seed of rules/allowed-terms.txt, case folded."""


def _allowed_candidate(value: str) -> bool:
    """A candidate that is a phrase of the allowed terms, or whose every word is one."""
    key = " ".join(re.findall(r"\w+", value.casefold()))
    return bool(key) and (key in _SHORT_ALLOWED or all(w in _SHORT_ALLOWED for w in key.split()))


def short_text_candidates(text: str) -> list[tuple[str, int]]:
    """(class, line) of each name-shaped candidate of a short text by SHORT_TEXT_RULES, the class person, company
    or unknown, the line counted from 1; a candidate of the allowed terms is none. The caller runs the register
    check first. Never a value."""
    out: list[tuple[str, int]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        norm = normalize.normalize(line).text
        for rule in SHORT_TEXT_RULES:
            if any(not _allowed_candidate(v) for v in wipe.unknown_candidates(norm, [], (rule,))):
                found = (_RULE_CLASS.get(rule, "person"), n)
                if found not in out:
                    out.append(found)
    return out


REWORD_HINT = ("reword it: a role or a code in place of the name; a technical term the rule takes for a name goes "
               "into the section intake-seed of rules/allowed-terms.txt (the word, or the phrase as written)")


# --------------------------------------------------------------------------- the keep list


def _sealed_keep(p: config.Paths) -> Path:
    return p.keep_list.with_name(p.keep_list.name + ".gpg")


def _vault_call(p: config.Paths, op: str, **fields) -> dict:
    """One request to the vault daemon for the keep list. Errors carry no value."""
    from awb import vault

    try:
        return vault.admin_call(op, p.admin_sock, **fields)
    except vault.VaultLocked:
        raise IntakeError("keep list: the vault is locked, run awb vault unlock") from None
    except vault.VaultUnavailable:
        raise IntakeError("keep list: no vault daemon") from None
    except vault.VaultBusy:
        raise IntakeError("keep list: the vault daemon is reloading, try again") from None
    except vault.VaultError:
        raise IntakeError("keep list: the vault daemon refused the request") from None


def _read_keep(p: config.Paths) -> str:
    """The text of the keep list: the sealed copy through the vault daemon when there is one, plus a plaintext
    copy when there is one. Missing -> empty."""
    parts: list[str] = []
    sealed = _sealed_keep(p)
    if sealed.exists():
        answer = _vault_call(p, "open_file", path=str(sealed))
        try:
            parts.append(base64.b64decode(answer.get("data") or "", validate=True).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise IntakeError("keep list: the vault daemon answered in an unexpected form") from None
    try:
        parts.append(p.keep_list.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except UnicodeDecodeError:
        raise IntakeError("keep list: the file is not UTF-8 text") from None
    return "\n".join(parts)


def _parse_keep(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        phrase = " ".join(line.split())
        key = keep_key(phrase)
        if phrase and key not in seen:
            seen.add(key)
            out.append(phrase)
    return out


def load_keep(p: config.Paths) -> list[str]:
    """The phrases of the keep list (`Paths.keep_list`, vault side), in the order they were added. One phrase per
    line. When the vault is encrypted the sealed copy is read through the vault daemon. Missing -> []."""
    return _parse_keep(_read_keep(p))


def _write_private(path: Path, text: str) -> None:
    """Atomic write, file mode 600. The folder is created with mode 700 when it is missing."""
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".keep-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def keep_phrase(p: config.Paths, phrase: str) -> tuple[bool, int]:
    """Add one phrase to the keep list. Returns (added, phrases in the list); added is False when the phrase was
    kept before. Any phrase is taken, only an empty one is an error. When the vault is encrypted the list is
    sealed again through the vault daemon and no plaintext copy stays behind. Nothing here echoes the phrase."""
    added, count = keep_phrases(p, [phrase])
    return added == 1, count


def keep_phrases(p: config.Paths, phrases: list[str]) -> tuple[int, int]:
    """Add several phrases to the keep list in one write and, when the vault is encrypted, one seal (the review
    of a blocked intake keeps hundreds at once). Returns (added, phrases in the list). A phrase kept before is
    left as it is; an empty one refuses the whole call before anything is written. Nothing here echoes a phrase."""
    clean = []
    for phrase in phrases:
        if not isinstance(phrase, str):
            raise IntakeError("keep list: the phrase must be text")
        phrase = " ".join(normalize.normalize(phrase).text.split())
        if not phrase:
            raise IntakeError("keep list: the phrase is empty")
        clean.append(phrase)
    kept = load_keep(p)
    keys = {keep_key(k) for k in kept}
    added = 0
    for phrase in clean:
        if keep_key(phrase) not in keys:
            kept.append(phrase)
            keys.add(keep_key(phrase))
            added += 1
    if not added:
        return 0, len(kept)
    encrypted = p.register_encrypted.exists() or _sealed_keep(p).exists()
    try:
        before = p.keep_list.read_bytes() if encrypted else None
    except FileNotFoundError:
        before = None
    _write_private(p.keep_list, "".join(k + "\n" for k in kept))
    if encrypted:
        try:
            _vault_call(p, "seal_file", path=str(p.keep_list))
        except BaseException:
            # no new plaintext stays behind; a plaintext copy that was there before is put back as it was
            if before is None:
                p.keep_list.unlink(missing_ok=True)
            else:
                _write_private(p.keep_list, before.decode("utf-8"))
            raise
    return added, len(kept)


# --------------------------------------------------------------------------- detection and replacement


def _resolve(names: list[Span], structured: list[Span]) -> list[Span]:
    """One list without overlaps. A structured hit that holds name hits wins over them (a mail address at a
    registered domain becomes one MAIL token) and a structured hit inside a name hit is dropped. Spans that
    still overlap (a partial overlap or a chain of them) are merged into one span over all of them, with the
    structured class when one of them is structured."""
    kept: dict[int, Span] = {id(s): s for s in names}
    extra: list[Span] = []
    for s in structured:
        over = [a for a in kept.values() if a.start < s.end and s.start < a.end]
        if over and any(a.start <= s.start and s.end <= a.end for a in over):
            continue
        for a in over:
            kept.pop(id(a), None)
        start = min([s.start] + [a.start for a in over])
        end = max([s.end] + [a.end for a in over])
        extra.append(Span(start, end, s.cls) if (start, end) != (s.start, s.end) else s)
    merged: list[Span] = []
    for s in sorted(list(kept.values()) + extra, key=lambda s: (s.start, s.end)):
        if merged and s.start < merged[-1].end:
            last = merged[-1]
            if last.cls == "name" and s.cls == "name":
                cls, code = "name", last.code
            else:
                cls, code = (last.cls if last.cls != "name" else s.cls), None
            merged[-1] = Span(last.start, max(last.end, s.end), cls, code)
        else:
            merged.append(s)
    return merged


def _canon(cls: str, value: str) -> str:
    """The key under which one structured value gets one token."""
    v = re.sub(r"\s+", "", value).casefold()
    if cls == "phone":
        v = ("+" if v.startswith("+") else "") + re.sub(r"\D", "", v)
    return v


class _Engine:
    """Detection, tokens and replacement for one run. Holds the only map from values to tokens: the codes of
    registered forms, the tokens of structured data and, in `wipe`, the tokens of the candidates and the forms the
    run learned."""

    def __init__(self, entries: list[register.Entry]):
        self.matcher = Matcher(register.forms_for_matching(entries))
        self.forms = len({(e.code, e.form) for e in entries if e.status == "active"})
        # short active forms: a fresh code must not carry one, not even inside the random part
        self._short = sorted({re.sub(r"[\W_]", "", e.form).casefold() for e in entries if e.status == "active"}
                             - {""}, key=len)
        self._short = [f for f in self._short if 3 <= len(f) <= 6]
        self.used: set[str] = set(register.codes(entries))
        self.tokens: dict[tuple[str, str], str] = {}
        self.hits: dict[tuple[str, str], _report.Hit] = {}
        self.wipe = wipe.WipeState()
        # the second check (T-13): every registered variant as a skeleton, compared over whole words
        self._skeletons = {s for s in (skeleton(v) for v, _ in register.forms_for_matching(entries))
                           if len(s) >= _SKELETON_MIN}
        self._skeleton_max = max((len(s) for s in self._skeletons), default=0)

    @property
    def keep(self) -> tuple[str, ...]:
        """The phrases of the keep list: never a candidate, never a learned form."""
        return self.wipe.keep

    @keep.setter
    def keep(self, phrases) -> None:
        self.wipe.keep = tuple(phrases)

    # tokens

    def safe(self, code: str) -> bool:
        """A code that no check would ever report: no register form in it and no structured shape."""
        low = code.casefold()
        if any(f in low for f in self._short):
            return False
        return not self.matcher.find(code) and not patterns.find_structured(code)

    def fresh(self, kind: str) -> str:
        for _ in range(1000):
            code = codes.new_code(kind, self.used)
            if self.safe(code):
                self.used.add(code)
                return code
        raise IntakeError("could not find a free %s code" % kind)

    def fresh_file_id(self, taken: set[str]) -> str:
        for _ in range(1000):
            fid = "F-" + "".join(secrets.choice(codes.ALPHABET) for _ in range(4))
            if fid not in taken and self.safe(fid):
                taken.add(fid)
                return fid
        raise IntakeError("could not find a free file id")

    def token_for(self, span: Span, value: str) -> str:
        if span.cls in wipe.WIPE_CLASSES:
            return wipe.token_for(self.wipe, span, value)
        if span.cls == "name" and span.code:
            return span.code
        key = (span.cls, _canon(span.cls, value))
        token = self.tokens.get(key)
        if token is None:
            token = self.fresh(span.cls.upper())
            self.tokens[key] = token
        return token

    def record(self, where: str, span: Span, value: str, token: str) -> None:
        key = (token, value.casefold())
        hit = self.hits.get(key)
        if hit is None:
            rule = ""
            if span.cls in wipe.WIPE_CLASSES:
                rule = wipe.LEARNED if (span.code or "").startswith("L:") else (span.form or "candidate")
            hit = self.hits[key] = _report.Hit(token=token, cls=span.cls, value=value, rule=rule)
        hit.count += 1
        hit.where.setdefault(where, []).append(span.start)

    def learned_forms(self) -> list[tuple[str, str, str]]:
        """(form, class, the value it came from) of every form the run learned, for the private report."""
        out = []
        for low, (word, key) in sorted(self.wipe.learned.words.items()):
            out.append((word, key[0], self.wipe.sources.get(low, "")))
        return out

    # spans

    def raw_spans(self, text: str) -> tuple[list[Span], list[Span]]:
        """Registered forms and structured data."""
        return self.matcher.find(text), patterns.find_structured(text)

    def spans(self, text: str, candidates: bool = True) -> tuple[list[Span], list[Span]]:
        """Registered forms, and structured data with the candidates and the learned forms of the run (wipe mode)."""
        names, structured = self.raw_spans(text)
        if not candidates:
            return names, structured
        return names, structured + wipe.wipe_spans(self.wipe, text, names, structured)

    def detect(self, where: str, text: str, names_only: bool = False) -> list[str]:
        """Record every registered form and piece of structured data of `text` with its token, learn the forms of
        its candidates and return their values (for `--review`). Positions are recorded in `text` as given. With
        `names_only` only registered forms are looked for and nothing is learned (for raw parts that no reader
        renders). A file name is scanned but teaches nothing (a file named like a closing is no signer)."""
        if not text:
            return []
        n = normalize.normalize(text)
        names = self.matcher.find(n.text)
        structured = [] if names_only else patterns.find_structured(n.text)
        for s in _resolve(names, structured):
            value = n.text[s.start:s.end]
            first = normalize.original_span(n, s.start, s.end)[0]
            self.record(where, Span(first, first + len(value), s.cls, s.code), value, self.token_for(s, value))
        if names_only or where.endswith("file name"):
            return []
        spans = wipe.learn(self.wipe, n.text, names, structured)
        out: list[str] = []
        seen: set[str] = set()
        for s in spans:
            value = re.sub(r"\s+", " ", n.text[s.start:s.end]).strip()
            if value and value.casefold() not in seen:
                seen.add(value.casefold())
                out.append(value)
        return out

    def sanitize(self, text: str, where: str) -> tuple[str, Counter]:
        """`text` with every hit replaced by its token, normalised: the registered forms by their codes, structured
        data, candidates and learned forms by their tokens; a [company] token right after the customer code merges
        into the code. Counts per class of what was replaced."""
        counts: Counter = Counter()
        if not text:
            return text, counts
        # an encoded block that decodes to text is replaced by that text first, so nothing encoded slips past;
        # bidi overrides and tag characters go, whether or not a hit follows: a viewer would render a reversed
        # or hidden text that no check saw
        text, _ = expand_base64_blocks(text)
        text = normalize.strip_invisible(text)
        for _ in range(MAX_PASSES):
            n = normalize.normalize(text)
            spans = _resolve(*self.spans(n.text))
            if not spans:
                break

            def token(s: Span, _t=n.text) -> str:
                value = _t[s.start:s.end]
                tok = self.token_for(s, value)
                self.record(where, s, value, tok)
                counts[s.cls] += 1
                # a token never opens a Markdown link: "[person 1](...)" would read as one
                return tok + " " if tok.startswith("[") and _t[s.end:s.end + 1] == "(" else tok

            text = self.matcher.replace(n.text, spans, token)
        return wipe.merge_customer_code(text), counts

    def final_hits(self, text: str, candidates: bool = True) -> list[Span]:
        """Every hit the name check would report in `text`, including inside decoded base64 and hex blocks
        and the printable strings of base64 blocks that decode to binary; with `candidates` also the candidates
        and the learned forms of the run."""
        n = normalize.normalize(text)
        names, structured = self.spans(n.text, candidates)
        hits = names + structured
        for block in encoded_texts(text):
            nb = normalize.normalize(block)
            a, b = self.spans(nb.text, candidates)
            hits.extend(a + b)
        return hits

    def second_hits(self, text: str, learned: bool = True) -> list[Span]:
        """The second check (T-13), by another method than the matcher: the words of the normalised text are
        folded to their letters and joined one after the other; a run of whole words that spells a registered
        variant (with `learned` also a form the run learned) is a hit, whatever stood between the words. A phrase
        of the keep list is left out. Also run over decoded base64 and hex blocks."""
        skeletons = self._skeletons
        if learned:
            skeletons = skeletons | wipe.learned_skeletons(self.wipe)
        if not skeletons:
            return []
        longest = max(len(s) for s in skeletons)
        keep_res = [re.compile(r"\s+".join(re.escape(w) for w in keep_key(k).split()), re.IGNORECASE)
                    for k in self.keep if keep_key(k)]
        hits: list[Span] = []
        for piece in [text] + list(encoded_texts(text)):
            n = normalize.normalize(piece).text
            kept = [(m.start(), m.end()) for r in keep_res for m in r.finditer(n)]
            words = [(m.start(), m.end(), _fold(m.group(0))) for m in _SKELETON_WORD_RE.finditer(n)]
            for i in range(len(words)):
                joined = ""
                for j in range(i, len(words)):
                    joined += words[j][2]
                    if len(joined) > longest:
                        break
                    if joined not in skeletons:
                        continue
                    start, end = words[i][0], words[j][1]
                    single = i == j
                    if single and len(joined) < _SKELETON_CASE_FREE and n[start:end].islower():
                        continue
                    if any(a <= start and end <= b for a, b in kept):
                        continue
                    hits.append(Span(start, end, SECOND_CLASS))
                    break
        return hits

    def leaks(self, text: str, candidates: bool = True) -> list[Span]:
        """What neither the matcher nor the second check may find in a written output; with `candidates` the
        candidate rules and the learned forms may find nothing either."""
        if candidates:
            return self.final_hits(text) + self.second_hits(text)
        return self.final_hits(text, candidates=False) + self.second_hits(text, learned=False)


# --------------------------------------------------------------------------- the run


def safe_new_code(entries: list[register.Entry], kind: str, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """A fresh top-level code (register.next_code) that carries no register form and no structured shape."""
    engine = _Engine(entries)
    engine.used |= set(taken)
    for _ in range(1000):
        code = register.next_code(list(entries), kind)
        if code not in engine.used and engine.safe(code):
            return code
    raise IntakeError("could not find a free %s code" % kind)


def issued_codes(p: config.Paths) -> set[str]:
    """Customer codes that earlier intakes used: the folders on both sides."""
    out: set[str] = set()
    for base in (p.private_reports, p.originals, p.outbox):
        try:
            out.update(d.name for d in base.iterdir() if d.is_dir() and codes.is_code(d.name))
        except OSError:
            pass
    return out


def _resolve_customer(customer: str, entries: list[register.Entry], p: config.Paths) -> tuple[str, bool]:
    if isinstance(customer, str) and codes.is_project_code(customer):
        # a project without a customer: its copies go out under the project code and nothing becomes a customer
        # code; registered forms of any customer still become their codes
        return customer, False
    if customer == NEW:
        return safe_new_code(entries, "CUST", issued_codes(p)), True
    if (not isinstance(customer, str) or not codes.is_code(customer) or customer.count("-") != 1
            or codes.kind_of(customer) != "CUST"):
        raise IntakeError("customer must be a top-level CUST code or new")
    states = {e.status for e in entries if e.code == customer}
    if states and "active" not in states:
        raise IntakeError("customer %s is retired in the register" % customer)
    if not states and customer not in issued_codes(p):
        raise IntakeError("customer %s is not in the register and no earlier intake issued it" % customer)
    return customer, False


def _taken_ids(p: config.Paths, customer: str) -> set[str]:
    out: set[str] = set()
    for base in (p.originals / customer, p.outbox / customer):
        try:
            for f in base.iterdir():
                m = FILE_ID_RE.match(f.name)
                if m:
                    out.add(m.group(0))
        except OSError:
            pass
    return out


def _meta_text(meta: dict) -> str:
    lines = []
    for key, value in meta.items():
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value)
        if isinstance(value, dict):
            value = " ".join("%s %s" % kv for kv in value.items())
        if isinstance(value, str) and value.strip():
            lines.append("%s: %s" % (key, value))
    return "\n".join(lines)


def _label_of(ex) -> str:
    return str(ex.meta.get("member") or ex.meta.get("attachment") or "")


def _walk(ex, part_id: str):
    """(part id, extraction) for an extraction and all its children, parent first."""
    yield part_id, ex
    for i, child in enumerate(ex.children or [], start=1):
        yield from _walk(child, "%s.%d" % (part_id, i))


def _suffix(name: str, engine: _Engine) -> str:
    """The original suffix when it is a plain short suffix that carries nothing, else none."""
    suffix = Path(name).suffix
    if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix) and not engine.final_hits(suffix[1:], candidates=False):
        return suffix.lower()
    return ""


def _output_text(record: _report.FileRecord, bodies: dict[str, str]) -> str:
    lines: list[str] = []
    for i, part in enumerate(record.parts):
        if i == 0:
            lines += ["# %s" % part.part_id, ""]
        else:
            lines += ["", "## %s" % part.part_id, ""]
        lines.append("- file id: %s" % part.part_id)
        lines.append("- kind: %s" % part.kind)
        lines.append("- state: %s" % part.state)
        lines.append("- notes: %s" % ("; ".join(part.public_notes) or "none"))
        body = bodies.get(part.part_id, "")
        if body.strip():
            lines += ["", body.strip("\n")]
    return "\n".join(lines) + "\n"


def vault_encrypted(p: config.Paths) -> bool:
    """True when the vault is encrypted: the register exists as register.tsv.gpg only."""
    try:
        return p.register_encrypted.is_file()
    except OSError:
        return False


def seal(p: config.Paths, path: Path) -> str | None:
    """Seal a plaintext file of the vault through the vault daemon (`seal_file`): it writes `<path>.gpg` and
    removes the plaintext. None when that worked, else a short reason without a value; the plaintext then stays
    where it is, inside the vault."""
    from awb import vault

    try:
        vault.admin_call("seal_file", p.admin_sock, path=str(path))
    except vault.VaultLocked:
        return "vault locked"
    except vault.VaultUnavailable:
        return "no vault daemon"
    except vault.VaultBusy:
        return BUSY_SEAL
    except vault.VaultError:
        return "refused by the vault daemon"
    return None


BUSY_SEAL = "the vault daemon is reloading, try again"
UNDO_SEALS = ("vault locked", BUSY_SEAL, "refused by the vault daemon")
"""Seal failures that undo the run: the daemon answered and would not seal. Without a daemon the plaintext stays
inside the vault and the run names it."""
UNDONE = ("the vault daemon is busy or refused the seal, nothing of this run stays in the vault in plaintext, run "
          "the intake again")


def _undo(p: config.Paths, cust: str, moved: list[tuple[Path, Path, str]], outputs: list[Path],
          extra: list[Path]) -> None:
    """Take a run back after a refused or busy seal: every original of the run goes back to where it came from (a
    sealed one through open_file; one that cannot be opened stays sealed, never in plaintext), the outputs, the
    pictures and the reports of the run are removed."""
    from awb import vault

    for src, dst, fid in moved:
        try:
            if _is_file(dst):
                shutil.move(str(dst), str(src))
            elif _is_file(_sealed(dst)):
                answer = vault.admin_call("open_file", p.admin_sock, path=str(_sealed(dst)))
                fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(base64.b64decode(answer.get("data") or ""))
                _sealed(dst).unlink()
        except (OSError, ValueError, vault.VaultError):
            pass
        shutil.rmtree(_images.folder(p, cust, fid), ignore_errors=True)
    for f in list(outputs) + list(extra):
        for g in (f, _sealed(f)):
            try:
                g.unlink()
            except OSError:
                pass


def _is_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _sealed(path: Path) -> Path:
    return path.with_name(path.name + ".gpg")


def _free_private_path(path: Path) -> Path:
    """`path` or path-2, path-3 ... when the name or its sealed copy is taken: sealing must never replace an
    older sealed report."""
    candidate, n = path, 1
    while candidate.exists() or _sealed(candidate).exists():
        n += 1
        candidate = path.with_name("%s-%d%s" % (path.stem, n, path.suffix))
    return candidate


def _write_private_report(p: config.Paths, path: Path, rec, encrypted: bool) -> tuple[Path, int]:
    """Write the private report and seal it when the vault is encrypted. Returns the path of the report as it
    lies in the vault and 1 when it stayed in plaintext, else 0."""
    written = _report.write_private(_free_private_path(path) if encrypted else path, rec)
    if not encrypted:
        return written, 0
    why = seal(p, written)
    if why in UNDO_SEALS:
        written.unlink(missing_ok=True)
        raise _Undo(written)
    if why is not None:
        return written, 1
    return _sealed(written), 0


class _Undo(Exception):
    """A private report whose seal was refused or busy: it is removed and the run is taken back."""

    def __init__(self, path: Path):
        super().__init__("undo")
        self.path = path


def _move_original(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dst.parent, 0o700)
    if dst.exists():
        raise FileExistsError(dst.name)
    shutil.move(str(src), str(dst))
    os.chmod(dst, 0o600)


def _readable(word: str, text: str) -> bool:
    return word.casefold() in text.casefold()


def selftest() -> list[str]:
    """Every case of awb.planted, run through an engine of its own with an invented register: the matcher must
    report the case's class and the second check must find the cases marked for it. After the sanitising
    neither may find anything. Then the wipe cases: each planted name must come out as a token of its class and
    nothing of it readable, every control term must come out untouched, and the self-test's customer inside a
    company shape must become the code. Returns the failures, empty when all pass (R-004, T-94)."""
    from awb import planted

    failures: list[str] = []
    engine = _Engine(planted.entries())
    for label, text, cls, second in planted.CASES:
        found = {h.cls for h in engine.final_hits(text)}
        if cls not in found:
            failures.append("%s: the matcher does not report %s" % (label, cls))
        if second and not engine.second_hits(text):
            failures.append("%s: the second check does not find it" % label)
        clean, _ = engine.sanitize(text, "selftest")
        if engine.leaks(clean):
            failures.append("%s: something is left after the sanitising" % label)
    if engine.leaks("release 1.4.2 for %s in tcp-q7m4, two app clusters, one per zone" % planted.CODE):
        failures.append("clean text: a check reports a hit")
    for label, text, words, cls in planted.WIPE_CASES:
        e = _Engine(planted.entries())
        e.detect("selftest detect", text)
        clean, _ = e.sanitize(text, "selftest")
        if any(_readable(w, clean) for w in words):
            failures.append("%s: a planted name is left after the sanitising" % label)
        if not re.search(r"\[%s \d+\]" % wipe.TOKEN_TEXT[cls if cls != "name" else "unknown"], clean):
            failures.append("%s: no %s token" % (label, cls))
        if e.leaks(clean):
            failures.append("%s: a check reports a hit after the sanitising" % label)
    for i, term in enumerate(planted.CONTROL_TERMS, start=1):
        e = _Engine(planted.entries())
        for text in ("Wir prüfen %s heute." % term, "| %s | 2 |" % term):
            e.detect("selftest detect", text)
            clean, _ = e.sanitize(text, "selftest")
            if term not in clean:
                failures.append("control term %d is wiped" % i)
                break
    e = _Engine(planted.entries())
    label, text = planted.CUSTOMER_CASE
    e.detect("selftest detect", text)
    clean, _ = e.sanitize(text, "selftest")
    if planted.CUSTOMER_CODE not in clean or "[" in clean or _readable(planted.CUSTOMER_SHORT, clean) \
            or _readable("Spedition", clean):
        failures.append("%s: the customer form does not become the code" % label)
    return failures


def run(files: list[Path], customer: str, p: config.Paths, *, review: bool = False, edit=None,
        confirm=None, redo: dict | None = None) -> IntakeResult:
    """Take `files` in for `customer` (a CUST code, "new" or the code of a project without a customer, tcp-xxxx)
    in wipe mode. See the module text for the steps.
    Nothing is read before the self-test passed. With `review` the run first stops on the candidates and hands
    them to the review of `awb register review` (its editor, marks, register and keep list; `edit` and `confirm`
    stand in for the editor and the question in the tests), then goes on in wipe mode with the register and the
    keep list of after the review: a candidate left for later is wiped like any other. `redo` maps each file to
    (file id, its original in the vault): a copy of an original taken out of the vault runs again under its file id,
    its output replaces the one in the outbox, and nothing moves into the vault (`awb import --redo`)."""
    files = [Path(f) for f in files]
    if not files:
        raise IntakeError("no files given")
    failed = selftest()
    if failed:
        raise IntakeError("the intake failed its self-test and takes nothing in: %s" % "; ".join(failed))
    for i, f in enumerate(files, start=1):
        # a link would move only the link and change the mode of what it points to
        if f.is_symlink():
            raise IntakeError("file %d of %d is a symbolic link, give the file itself" % (i, len(files)))
        if not f.is_file():
            raise IntakeError("file %d of %d is not a regular file" % (i, len(files)))
    if len({f.resolve() for f in files}) != len(files):
        raise IntakeError("the same file is given more than once")

    config.ensure_layout(p)
    from awb import vault

    # one intake at a time and never while `awb vault encrypt` runs: an original written in plaintext after the
    # encryption passed its folder would stay in plaintext
    reviewed = 0
    try:
        if review:
            with vault.vault_lock(p):
                customer, report, reviewed = _review_stop(files, customer, p)
            if reviewed:
                from awb import candidates

                try:
                    candidates.review(p, customer, report, edit=edit, confirm=confirm, next_step=False)
                except candidates.ReviewError as err:
                    raise IntakeError("review: %s" % err) from None
        with vault.vault_lock(p):
            return _run(files, customer, p, reviewed=reviewed, redo=redo)
    except vault.VaultError as err:
        raise IntakeError(str(err)) from None


def _detect_all(files: list[Path], customer: str, p: config.Paths, redo: dict | None = None):
    """Steps 2 to 4: the customer, file ids, extraction, detection and the forms the run learns. No stop."""
    entries = register.load(p.register)
    cust, is_new = _resolve_customer(customer, entries, p)
    engine = _Engine(entries)
    engine.keep = tuple(load_keep(p))
    now = datetime.now()
    rec = _report.Run(date=now.date().isoformat(), time=now.strftime("%H:%M:%S"), customer=cust,
                      new_customer=is_new, register_forms=engine.forms)
    taken = _taken_ids(p, cust)
    extractions: dict[str, list[tuple[str, object]]] = {}
    for src in files:
        if redo is not None:
            fid = redo[src][0]
            if not FILE_ID_RE.fullmatch(fid):
                raise IntakeError("a file of the redo has no file id")
        else:
            fid = engine.fresh_file_id(taken)
        with _extract.temp_root(p.vault / "tmp"):
            ex = _extract.extract(src)
        fr = _report.FileRecord(file_id=fid, original_name=src.name, kind=ex.kind, state=ex.state)
        name_text = re.sub(r"_+", " ", src.name)
        engine.detect("%s file name" % fid, name_text)
        parts = list(_walk(ex, fid))
        extractions[fid] = parts
        for part_id, pex in parts:
            label = src.name if part_id == fid else _label_of(pex)
            fr.parts.append(_report.Part(part_id=part_id, label=label, kind=pex.kind, state=pex.state,
                                         notes=list(pex.notes), meta=dict(pex.meta)))
            pieces = [pex.detect_text or ""]
            if pex.text and pex.text not in pieces[0]:
                pieces.append(pex.text)
            pieces.append(_meta_text(pex.meta))
            for c in engine.detect("%s detect" % part_id, "\n".join(t for t in pieces if t)):
                rec.candidates.append((part_id, c))
            engine.detect("%s raw parts" % part_id, getattr(pex, "scan_text", "") or "", names_only=True)
        rec.files.append(fr)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for pid, c in rec.candidates:
        if c.casefold() not in seen:
            seen.add(c.casefold())
            unique.append((pid, c))
    rec.candidates = unique
    return cust, engine, rec, extractions, now


def _review_stop(files: list[Path], customer: str, p: config.Paths) -> tuple[str, Path, int]:
    """The stop of `--review`: detection only, the candidates into a private report for the review; nothing is
    written to the outbox and no original moves. Returns the customer code, the report and the number of
    candidates."""
    encrypted = vault_encrypted(p)
    cust, engine, rec, _, now = _detect_all(files, customer, p)
    rec.stopped = True
    rec.hits = list(engine.hits.values())
    path = p.private_reports / cust / ("%s.md" % now.strftime("%Y-%m-%d-%H%M%S"))
    try:
        written, _ = _write_private_report(p, path, rec, encrypted)
    except _Undo:
        raise IntakeError(UNDONE) from None
    return cust, written, len(rec.candidates)


def _run(files: list[Path], customer: str, p: config.Paths, *, reviewed: int = 0,
         redo: dict | None = None) -> IntakeResult:
    # an encrypted register is read through the vault daemon; originals and private reports are then sealed
    encrypted = vault_encrypted(p)
    cust, engine, rec, extractions, now = _detect_all(files, customer, p, redo)
    rec.reviewed = reviewed
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    private_path = p.private_reports / cust / ("%s.md" % stamp)
    public_path = p.outbox / cust / _report.PUBLIC_NAME

    # steps 6 and 7: sanitise, write, check each output once more
    outdir = config.make_dir(p.outbox / cust, 0o750, shared=True)
    outputs: list[Path] = []
    check_failed: set[str] = set()
    try:
        _write_outputs(rec, extractions, engine, outdir, outputs, check_failed, replace=redo is not None)
    except BaseException:
        # never leave a half run behind: the outputs of this run go, the originals stay in the inbox
        for out in outputs:
            out.unlink(missing_ok=True)
        raise

    # step 8: move the originals of every file that passed the final check; seal them in an encrypted vault
    unsealed = 0
    pictures_total = 0
    moved: list[tuple[Path, Path, str]] = []
    for src, fr in zip(files, rec.files):
        if redo is not None:
            # the original has lain in the vault since its first import, its pictures are held
            fr.original = redo[src][1]
            if fr.file_id in check_failed:
                fr.reasons.append("the redo of this file failed the final check, no copy in the outbox")
            continue
        if fr.file_id in check_failed:
            fr.reasons.append("original left in the inbox because the final check failed")
            continue
        dst = p.originals / cust / ("%s%s" % (fr.file_id, _suffix(src.name, engine)))
        try:
            pictures = _images.hold(p, cust, fr.file_id, src, fr.kind, encrypted=encrypted)
        except (_images.ImageError, OSError) as err:
            pictures = 0
            fr.reasons.append("pictures not held (%s)" % type(err).__name__)
        if pictures:
            pictures_total += pictures
            fr.parts[0].public_notes.append("%d picture(s) held on the vault side, released only by awb images "
                                            "release after a look" % pictures)
        try:
            _move_original(src, dst)
            fr.original = dst
        except OSError as err:
            fr.reasons.append("original not moved (%s)" % type(err).__name__)
            continue
        moved.append((src, dst, fr.file_id))
        if encrypted:
            why = seal(p, dst)
            if why is None:
                fr.original = _sealed(dst)
            elif why in UNDO_SEALS:
                _undo(p, cust, moved, outputs, [])
                raise IntakeError(UNDONE)
            else:
                unsealed += 1
                fr.reasons.append("original not sealed (%s), it lies in plaintext in the vault" % why)

    # step 9: reports
    rec.hits = list(engine.hits.values())
    rec.learned = engine.learned_forms()
    public_text = _public_text(rec, engine)
    public_written = _report.write_public(public_path, public_text)
    try:
        private_written, report_unsealed = _write_private_report(p, private_path, rec, encrypted)
    except _Undo as undo:
        _undo(p, cust, moved, outputs, [public_written, undo.path])
        raise IntakeError(UNDONE) from None
    from awb import intake_counts
    intake_counts.record_intake(p, cust, len(rec.candidates))
    wiped: Counter = Counter()
    for f in rec.files:
        for part in f.parts:
            for cls, n in part.counts.items():
                if cls in wipe.WIPE_CLASSES:
                    wiped[wipe.TOKEN_TEXT[cls]] += n
    return IntakeResult(customer=cust, outputs=outputs, public_report=public_written,
                        private_report=private_written, states={f.file_id: f.state for f in rec.files},
                        unsealed=unsealed + report_unsealed, wiped=wiped, held=sorted(check_failed),
                        pictures=pictures_total, reviewed=reviewed, candidates=len(rec.candidates))


def _public_text(rec: _report.Run, engine: _Engine) -> str:
    """The public report, checked like an output against the registered forms and the structured patterns only:
    it carries no customer text, and a learned word its own template carries (Candidates, Files) must not
    withhold it (decision 22). Notes that carry a hit are withheld; when the report still carries one, only the
    counts go out."""
    text = _report.render_public(rec)
    if not engine.leaks(text, candidates=False):
        return text
    for fr in rec.files:
        for part in fr.parts:
            part.public_notes = ["notes withheld, see the private report"] if part.notes else []
    text = _report.render_public(rec)
    if not engine.leaks(text, candidates=False):
        return text
    text = _report.render_minimal(rec)
    if engine.leaks(text, candidates=False):
        raise IntakeError("the public report of %s does not pass the final check" % rec.customer)
    return text


_HEADER_LINE_RE = re.compile(r"(?m)^(?:#{1,2} F-[A-Z2-7]{4}(?:\.\d+)*|- (?:file id|kind|state): [^\n]*)$")
_NOTES_LINE_RE = re.compile(r"(?m)^- notes: ")
_READER_MARKS_RE = re.compile(r"[*`|]|</?[A-Za-z][^<>\n]{0,80}>")


def _body_view(text: str) -> str:
    """An output without the header lines the intake writes itself (`# F-ABCD`, `- file id:` ...); the notes stay
    as text. The candidate rules never read those lines: a header is no speaker label."""
    return _NOTES_LINE_RE.sub("", _HEADER_LINE_RE.sub("", text))


def _body_hits(engine: _Engine, text: str) -> list[Span]:
    """The candidates and learned forms left in the body of an output, and the learned forms in the body as a reader
    reads it (marks and table borders as spaces, so that cells join). Candidates of the joined view do not count:
    two cells side by side are no run of a sentence, and the column rules have read the table."""
    body = _body_view(text)
    n = normalize.normalize(body).text
    names, structured = engine.raw_spans(n)
    hits = wipe.wipe_spans(engine.wipe, n, names, structured)
    reader = _READER_MARKS_RE.sub(lambda m: " " * len(m.group(0)), n)
    if reader != n:
        names, structured = engine.raw_spans(reader)
        hits += [s for s in wipe.wipe_spans(engine.wipe, reader, names, structured)
                 if (s.code or "").startswith("L:")]
    return hits


def _write_outputs(rec, extractions, engine: _Engine, outdir: Path, outputs: list[Path],
                   check_failed: set[str], replace: bool = False) -> None:
    """Steps 6 and 7: sanitise every file, write its output and check it before and after writing: the
    registered forms, the structured data and the second check over the whole text, the candidate rules and the
    learned forms over the body."""
    for fr in rec.files:
        bodies: dict[str, str] = {}
        by_id = dict(extractions[fr.file_id])
        for part in fr.parts:
            pex = by_id[part.part_id]
            body, counts = engine.sanitize(pex.text or "", "%s output" % part.part_id)
            bodies[part.part_id] = body
            part.counts.update(counts)
            for note in part.notes:
                clean, _ = engine.sanitize(note, "%s note" % part.part_id)
                part.public_notes.append(clean)
        if not any(b.strip() for b in bodies.values()):
            fr.reasons.append("no text to write, no output")
            continue
        out = outdir / ("%s.md" % fr.file_id)
        text = _output_text(fr, bodies)
        hits = engine.leaks(text, candidates=False) + _body_hits(engine, text)
        if not hits:
            if replace:
                written = _report.write_replace(out, text, 0o640, 0o750)
            else:
                written = _report.write_new(out, text, 0o640, 0o750)
            if written != out:  # the file id was taken meanwhile: never write under another name
                written.unlink(missing_ok=True)
                raise IntakeError("output %s already exists" % fr.file_id)
            outputs.append(out)   # from here on a failure of the run removes it again
            written_text = out.read_text(encoding="utf-8")
            hits = engine.leaks(written_text, candidates=False) + _body_hits(engine, written_text)
            if hits:
                out.unlink(missing_ok=True)
                outputs.remove(out)
        if hits:
            check_failed.add(fr.file_id)
            fr.state = "failed"
            classes = _report.counts_text(Counter(h.cls for h in hits))
            fr.reasons.append("final check found %s, output deleted" % classes)
            fr.parts[0].public_notes.append("final check failed, output deleted")
            continue
        fr.output = out


def wipe_text(text: str, p: config.Paths) -> str:
    """One text through wipe mode without an import (the task description of a lab project, decision 16): an
    engine of its own with the register and the keep list, the forms its candidates teach, then the sanitising.
    The caller has checked the text against the register before; the self-test runs first."""
    failed = selftest()
    if failed:
        raise IntakeError("the intake failed its self-test: %s" % "; ".join(failed))
    engine = _Engine(register.load(p.register))
    engine.keep = tuple(load_keep(p))
    engine.detect("text detect", text)
    clean, _ = engine.sanitize(text, "text output")
    if _body_hits(engine, clean) or engine.leaks(clean, candidates=False):
        raise IntakeError("the final check found something left in the text")
    return clean


def wiped_text(counts: Counter) -> str:
    """The wiped values per token class in one line: "person 3, company 1, name 2", or "nothing"."""
    items = [(word, counts.get(word, 0)) for word in ("person", "company", "place", "name")]
    return ", ".join("%s %d" % kv for kv in items if kv[1]) or "nothing"


def inbox_files(p: config.Paths) -> list[Path]:
    """Every regular, not hidden file directly in the vault inbox, sorted."""
    try:
        return sorted(f for f in p.inbox.iterdir() if f.is_file() and not f.name.startswith("."))
    except OSError:
        return []
