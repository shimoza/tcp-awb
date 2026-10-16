"""The engine of the intake: detection, tokens and replacement for one run (`_Engine`), the candidates the rest of
the Workbench asks for (`short_text_candidates`, `unknown_candidates`) and the final check over the body of an
output (`_body_hits`). One part of the intake package (F1 step 8); the run and the package text are awb/intake.py,
the candidate rules awb/intake/wipe/. Nothing in this module writes a matched value or a form into an exception or
a log line.
"""
from __future__ import annotations

import re
import secrets
from collections import Counter

from awb import codes, normalize, patterns, register
from awb import report as _report
from awb.extract.text import encoded_texts, expand_base64_blocks
from awb.intake import wipe
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

class IntakeError(Exception):
    """The intake cannot run. The message carries codes, counts and positions, never a name or a value."""



# --------------------------------------------------------------------------- candidates

# the candidate rules live in awb/intake/wipe/; these names are the interface the rest of the Workbench uses
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



# --------------------------------------------------------------------------- the final check of an output

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
