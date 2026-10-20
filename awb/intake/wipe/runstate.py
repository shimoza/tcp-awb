"""The vocabulary, one run (WipeState, its tokens and `bind`) and the interface of the intake (`collect`,
`candidate_spans`, `wipe_spans`, `merge_customer_code`, `keep_key`). One family of the wipe rules (F1 step 8); the
package text is awb/intake/wipe/__init__.py.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from awb import codes, normalize
from awb import words as _word_lists
from awb.intake.wipe.basic import _COMPANY_RE, _Mask, _SPACED_RE, _TITLES, _mail_candidates, _spaced_words, _stop_spans
from awb.intake.wipe.lexicon import (_CODE_KIND_WORDS, _EXTRA_KNOWN, _FIRST_NAMES, _GENERIC_COMPANY_WORDS, _GERMAN_ADJ,
                                     _GERMAN_FUNCTION, _GERMAN_KNOWN, _PARTICLE_WORDS, _PRODUCT_LABEL_RE,
                                     _generic_local, _generic_word, _known_word, _letters)
from awb.intake.wipe.lexicon import _TECH_FORMS, _TECH_MULTI, _code_spans, _plain_value, _tech_spans
from awb.intake.wipe.proposed import (PROPOSED_RULES, _DC_RE, _blank_marks, _name_shaped, _plausible, _run_rule_exempt,
                                      _token_spans, _trim_run, _value_words)
from awb.intake.wipe.learned import _Learned, _TITLES_EXTRA, _domain_label, _host_labels
from awb.matcher import Span


# --------------------------------------------------------------------------- the vocabulary

_CORPUS = frozenset(_word_lists.load(_word_lists.WORDS_FILE))
"""The capitalised words of the public corpus (rules/known-words.txt from two files on)."""
_PHRASES = frozenset(_word_lists.load(_word_lists.PHRASES_FILE)) | _TECH_MULTI
_VOCAB = _CORPUS | _GERMAN_KNOWN | _GERMAN_FUNCTION | _EXTRA_KNOWN | _GERMAN_ADJ | _TECH_FORMS
_RUN_EXEMPT: set = set()
"""Casefolded words of the values under product labels (Hersteller: Kemp, Tools: Gitea, Zammad) of the run that is
bound now (`bind`)."""
_TITLES_WIPE = _TITLES | _TITLES_EXTRA


# --------------------------------------------------------------------------- one run

WIPE_CLASSES = ("person", "company", "place", "unknown")
TOKEN_TEXT = {"person": "person", "company": "company", "place": "place", "unknown": "name"}
TOKEN_RE = re.compile(r"\[(person|company|place|name)(?: \d+)?\]")
LEARNED = "learned form"
RULE_LABELS = {
    "_firma_candidates": "Firma word or Russian legal form",
    "_company_candidates": "legal form",
    "_lower_company_candidates": "legal form after lower-case words",
    "_label_candidates": "company label",
    "_label_value_candidates": "label",
    "_table_candidates": "table column",
    "_fixed_table_candidates": "fixed-width table column",
    "_header_candidates": "mail header",
    "_reply_candidates": "reply line",
    "_salutation_candidates": "salutation",
    "_signoff_candidates": "sign-off",
    "_speaker_candidates": "speaker label",
    "_address_candidates": "postal address",
    "_spaced_after_title_candidates": "title before spaced letters",
    "_title_candidates_cut": "title",
    "_particle_candidates_known": "particle",
    "_von_unknown_candidates": "known noun von unknown word",
    "_vocative_candidates": "vocative",
    "_initial_candidates_known": "initial",
    "_initials_candidates": "initials",
    "_cn_candidates": "calendar common name",
    "_byline_candidates": "by-line",
    "_subject_candidates": "subject or heading",
    "_run_candidates": "run of capitalised words",
    "_inverted_candidates": "Last, First",
    "_spaced_candidates": "spaced letters",
    "_identifier_run_candidates": "identifier",
    "_two_line_name_candidates": "names over two lines",
    "_syslog_candidates": "syslog line",
    "_inventory_candidates": "inventory or users list",
    "_hyphen_break_candidates": "hyphen at a line end",
    "_lone_line_candidates": "lone line before a company or role line",
    "_lone_first_name_candidates": "lone first name",
    "_login_candidates": "login or handle",
}
"""The rule of each span in words, for the reports: the private one lists every wiped value with it, the public one
counts per class how often each rule fired."""


@dataclass(frozen=True)
class WipeSpan:
    """One candidate of a text: positions in the text, its class and the rule that found it."""

    start: int
    end: int
    cls: str
    rule: str


class WipeState:
    """What one import run knows beyond the register: the tokens it issued, the forms it learned, the words of its
    product labels and the keep list. One per engine; `bind` makes it the one the rules read."""

    def __init__(self, keep: tuple[str, ...] = (), trusted: bool = False):
        self.trusted = trusted   # `awb import --trusted`: no guessed name is wiped, of any class
        self.tokens: dict[tuple[str, str], str] = {}
        self.numbers: Counter = Counter()
        self.learned = _Learned()
        self.run_exempt: set[str] = set()
        self.keep: tuple[str, ...] = tuple(keep)
        self.sources: dict[str, str] = {}   # learned word (case folded) -> the value it came from, private report


def bind(state: WipeState) -> None:
    """The rules read the product-label words of this run (`_known_word`)."""
    global _RUN_EXEMPT
    _RUN_EXEMPT = state.run_exempt


def _canon(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def token_for_key(state: WipeState, key: tuple[str, str]) -> str:
    """The token of one value: one number per value and class in the run; a person value that shares a word of four
    letters or more with an earlier person gets that person's number."""
    tok = state.tokens.get(key)
    if tok is None:
        cls, canon = key
        if cls == "person":
            words = {w for w in re.split(r"[\s'’.-]+", canon) if _letters(w) >= 4}
            for (k_cls, k_canon), k_tok in state.tokens.items():
                if k_cls == "person" and words & {w for w in re.split(r"[\s'’.-]+", k_canon) if _letters(w) >= 4}:
                    tok = k_tok
                    break
        if tok is None:
            state.numbers[cls] += 1
            tok = "[%s %d]" % (TOKEN_TEXT.get(cls, "name"), state.numbers[cls])
        state.tokens[key] = tok
    return tok


def token_for(state: WipeState, span: Span, value: str) -> str:
    """The token of a candidate span or a learned form (whose code carries the key of its source)."""
    if span.code and span.code.startswith("L:"):
        _, cls, canon = span.code.split(":", 2)
        return token_for_key(state, (cls, canon))
    return token_for_key(state, (span.cls, _canon(value)))


def candidate_spans(state: WipeState, text: str, known: list[Span]) -> list[Span]:
    """The candidate spans of `text` with their classes and rules (in `form`), without overlaps, keep phrases dropped,
    codes, tokens and product-label values never inside."""
    if not text:
        return []
    bind(state)
    raw = text
    text = _blank_marks(text)
    extra = _token_spans(text) + [Span(m.start("v"), m.end("v"), "token") for m in _PRODUCT_LABEL_RE.finditer(text)]
    extra += _code_spans(text) + _tech_spans(text)   # code and vendor terms (KVM, Sapphire Rapids) give no candidates
    blocked = _Mask(len(text), known + _stop_spans(text) + extra)
    found: list[tuple[int, int, int, str]] = []
    for prio, rule in enumerate(PROPOSED_RULES):
        for a, b, cls in rule(text, blocked):
            pieces = [(a, b)]
            spaced = rule.__name__ == "_spaced_candidates"
            if cls == "unknown" and not spaced and re.search(r"[ \t]{3,}", text[a:b]):
                cut = []
                pos = a
                for m in re.finditer(r"[ \t]{3,}", text[a:b]):
                    cut.append((pos, a + m.start()))
                    pos = a + m.end()
                cut.append((pos, b))
                # columns of a pdftotext table: every piece keeps two words, else the gap is just wide spacing
                if all(len(_value_words(text, x, y)) >= 2 for x, y in cut):
                    pieces = cut
            for x, y in pieces:
                if spaced:
                    joined = " ".join(_spaced_words(text[x:y]))
                    if _run_rule_exempt(joined, 0, len(joined)):
                        continue
                    jw = joined.split()
                    cls2 = "person" if jw and jw[0].casefold() in _FIRST_NAMES else cls
                    found.append((prio, x, y, cls2))
                    continue
                if cls == "unknown":
                    if _run_rule_exempt(text, x, y):
                        continue
                    x, y, cls2 = _trim_run(text, x, y)
                    if x is None:
                        continue
                    found.append((prio, x, y, cls2))
                    continue
                found.append((prio, x, y, cls))
    kept: list[tuple[int, int, str, int]] = []
    for prio, a, b, cls in sorted(found):
        if not _plausible(text, a, b, cls):
            continue
        if a < b and all(b <= ka or a >= kb for ka, kb, _, _ in kept):
            kept.append((a, b, cls, prio))
    keep_keys = {keep_key(k) for k in state.keep}
    learned = state.learned.words
    out: list[Span] = []
    text = raw
    for a, b, cls, prio in sorted(kept):
        # a span that starts or ends on a blanked mark is trimmed to the words
        while a < b and not text[a].strip():
            a += 1
        while b > a and not text[b - 1].strip():
            b -= 1
        value = re.sub(r"\s+", " ", text[a:b]).strip()
        if not value or keep_key(value) in keep_keys:
            continue
        if cls == "unknown" and _plain_value(value) and not (learned and value.casefold() in learned):
            continue   # Outcome, Worked, SCORECARD: a plain English word on its own is no name
        if cls == "unknown" and learned:
            classes = {learned[w.casefold()][1][0] for w in value.split() if w.casefold() in learned}
            if "person" in classes:
                cls = "person"
            elif "company" in classes:
                cls = "company"
        if cls == "unknown":
            ws = value.split()
            if 2 <= len(ws) <= 3 and ws[0].casefold().strip(".") in _FIRST_NAMES and all(_name_shaped(w) for w in ws):
                cls = "person"
            elif 2 <= len(ws) <= 3 and any(w.casefold().endswith(("ович", "евич", "овна", "евна", "ична", "инична")) for w in ws):
                cls = "person"
            elif len(ws) >= 2 and ws[-1].casefold() in _GENERIC_COMPANY_WORDS | {"systemhaus", "software", "solutions"}:
                cls = "company"
            elif len(ws) >= 2 and ws[0].casefold() in ("stadt", "landkreis", "gemeinde", "kreis", "land", "bezirk",
                                                      "universität", "hochschule", "klinikum", "sparkasse", "stadtwerke",
                                                      "firma", "fa.", "unternehmen", "verein", "verband", "stiftung"):
                cls = "company"
        out.append(Span(a, b, cls, None, RULE_LABELS.get(PROPOSED_RULES[prio].__name__, "candidate")))
    return out


def wipe_spans(state: WipeState, text: str, names: list[Span], structured: list[Span]) -> list[Span]:
    """The candidates of `text` and the learned forms of the run that stand in it, none over a registered form or a
    piece of structured data."""
    spans = candidate_spans(state, text, names + structured)
    if state.trusted:
        spans = []   # the owner vouches for the files: no guessed name; register forms and structured data stay
    taken_spans = names + structured + spans

    def taken(a: int, b: int) -> bool:
        return any(a < s.end and s.start < b for s in taken_spans)

    return spans + state.learned.find(text, taken, keep=state.keep)


def _person_like_run(value: str) -> bool:
    words = [w for w in value.split() if w.casefold() not in _PARTICLE_WORDS]
    if not 2 <= len(words) <= 3 or not all(_name_shaped(w) for w in words):
        return False
    if any(w.isupper() and len(w) <= 3 or codes.is_code(w) or w.casefold() in _CODE_KIND_WORDS for w in words):
        return False   # CUST FILEID: codes and their kinds are no person; XQARV POMBLET in capitals is
    if words[0].casefold().strip(".") in _FIRST_NAMES and not words[0].isupper():
        return True
    return all(not _known_word(w, suffix=False) and not _generic_word(w) for w in words)


def _learn(state: WipeState, cls: str, value: str, key: tuple[str, str], unknown_only: bool = False) -> None:
    before = set(state.learned.words)
    state.learned.learn(cls, value, key, unknown_only=unknown_only)
    for w in set(state.learned.words) - before:
        state.sources[w] = value


def learn(state: WipeState, text: str, names: list[Span], structured: list[Span]) -> list[Span]:
    """Detection of one text (normalised): the words of its product labels become terms of the run, the values of
    its person and company candidates, its person-like runs, the parts of person-like mail local parts and the
    labels of its mail and URL hosts become learned forms. Returns the candidate spans."""
    bind(state)
    state.learned.texts.append(text)
    for m in _PRODUCT_LABEL_RE.finditer(text):
        state.run_exempt.update(w.casefold() for w in re.findall(r"[^\W\d_][\w'’-]{2,}", m.group("v"))
                                if not codes.is_code(w))
    spans = candidate_spans(state, text, names + structured)
    for s in spans:
        value = text[s.start:s.end]
        if _SPACED_RE.fullmatch(value):
            value = " ".join(_spaced_words(value))   # a letter spaced stretch teaches its joined words
            if _COMPANY_RE.search(value):
                _learn(state, "company", value, ("company", _canon(value)))
                continue
            if s.cls == "unknown" and 2 <= len(value.split()) <= 3 and all(_name_shaped(w) for w in value.split()):
                _learn(state, "unknown", value, ("unknown", _canon(value)))
                continue
        if s.cls in ("person", "company"):
            _learn(state, s.cls, value, (s.cls, _canon(value)))
        elif s.cls == "unknown" and _person_like_run(value):
            # a run that looks like a person (a first name first, or unknown words only): its words are learned
            # with the run's token, so the surname of a full name is wiped where it stands alone later
            _learn(state, "unknown", value, ("unknown", _canon(value)), unknown_only=True)
    # the person-like local parts of mail addresses name the people of a mail chain
    for a, b in _mail_candidates(text, _Mask(len(text), names)):
        local = text[a:b]
        _learn(state, "person", " ".join(p for p in re.split(r"[._-]", local) if p), ("person", _canon(local)))
    # the distinctive label of a mail host names the company behind it; the other labels of a mail or URL host
    for s in structured:
        value = text[s.start:s.end]
        # a mail address, a URL with a scheme or a bare host of three labels or more names a host; a bare
        # "intake.run" of two labels may be code or a file and teaches nothing
        bare_labels = len([l for l in re.sub(r"^[a-z]+://", "", value, flags=re.I).split("/")[0].split("@")[-1]
                           .strip(".").split(".") if l])
        if s.cls == "mail" or (s.cls == "url" and (re.match(r"(?i)[a-z]+://|www\.", value) or bare_labels >= 3)):
            label = _domain_label(value) if s.cls == "mail" and not _generic_local(value) else None
            if label:
                _learn(state, "company", label, ("company", label.casefold()))
            for other in _host_labels(value):
                _learn(state, "unknown", other, ("unknown", other))
    for dc in _DC_RE.finditer(text):
        host = ".".join(x.strip() for x in re.findall(r"(?i)DC=([^,\n]+)", dc.group(0)))
        label = _domain_label("x@" + host) if host.count(".") >= 1 else None
        if label:
            _learn(state, "company", label, ("company", label.casefold()))
    return spans


_CODE_MERGE_RE = re.compile(r"(?<![\w-])(" + codes.CUST + r")[ \t]+\[company(?: \d+)?\]")


def merge_customer_code(text: str) -> str:
    """A [company] token right after the customer code is a longer form of the customer: one code. A [name] after
    the code is not merged ("CUST-Q7M4 Technische Zielarchitektur" keeps its heading)."""
    return _CODE_MERGE_RE.sub(r"\1", text)


def learned_skeletons(state: WipeState) -> set[str]:
    """The learned forms for the second check: the letters of every learned word of five letters or more."""
    return state.learned.skeletons()


# --------------------------------------------------------------------------- the interface of the intake

def collect(text: str, known: list[Span], state: WipeState | None = None) -> list[WipeSpan]:
    """The candidates of `text` (positions in `text`) that `known` does not cover, with their class and rule. With
    a `state` its learned forms are found too and the keep list of the state applies."""
    if not text:
        return []
    state = state if state is not None else WipeState()
    names = [s for s in known if s.cls == "name"]
    structured = [s for s in known if s.cls != "name"]
    spans = wipe_spans(state, text, names, structured)
    return sorted((WipeSpan(s.start, s.end, s.cls, s.form or LEARNED) for s in spans), key=lambda w: (w.start, w.end))


def wipe_values(text: str, known: list[Span], keep=()) -> list[str]:
    """The values of `collect`, ordered by position, without duplicates (case aside)."""
    out: list[str] = []
    seen: set[str] = set()
    for w in collect(text, known, WipeState(tuple(keep))):
        value = re.sub(r"\s+", " ", text[w.start:w.end]).strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)
    return out


def keep_key(phrase: str) -> str:
    """The form under which a phrase and a candidate are compared: normalised, whitespace runs as one space,
    case folded."""
    return " ".join(normalize.normalize(phrase).text.split()).casefold()
