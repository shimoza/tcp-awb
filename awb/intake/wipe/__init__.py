"""The candidate rules of the intake and the forms they teach (wipe mode, T4 of 2026-10-08).

The intake replaces every registered form by its code (the matcher) and every piece of structured data by its
token (the patterns). The rules here find what neither knows: strings shaped like the name of a person, a company or
a place. In wipe mode every string they recognise becomes a neutral token of its class, without a stop:

    person    [person 1]   a title, a salutation, a sign-off, a mail header, a speaker label, a person label or
                           column, a by-line, an initial, "Last, First", a login, a first name of the list ...
    company   [company 1]  a legal form, a company label or column, "Firma X", a Russian legal form, the domain of a
                           mail address whose local part names a person
    place     [place 1]    a street and number, a postcode and town, a place label or column
    unknown   [name 1]     a run of capitalised words that no list knows, an identifier, a lone word in a heading

The numbers count per class within one import run and restart with the next one: they tell two persons of one mail
chain apart and link nothing across imports. `collect` gives the spans of a text with their class and the rule that
found them (`WipeSpan`); `WipeState` holds what one run learned: the forms of every wiped person and company, matched
over every output of the run like a registered form (whole word, glued into an identifier, with German and Russian
endings), the tokens issued and the product-label words of the run.

The lists that keep a text readable are rules files: the public corpus (rules/known-words.txt and
rules/known-phrases.txt, written by `awb words update`), rules/known-words-de.txt, rules/known-words-ru.txt,
rules/first-names.txt, rules/source-platforms.txt, rules/standards.txt, the section german-function of
rules/sentence-openers.txt and the section intake-seed of rules/allowed-terms.txt. A missing list gives fewer
exemptions, never more: more is wiped, nothing passes that would not pass with the list.

The rules of the first release (the legal form, the company label, the mail local part, First von Last, the run, Last
First, the title, the initial, the spaced heading) live here too; `unknown_candidates` with `rules` runs them alone,
the way the goal check of `awb spawn` does. The reference for every rule of wipe mode is the prototype of the red team
of 2026-10-07 (presentations/names/proto/wipe_proto.py, not shipped); presentations/names/REDTEAM.md names the case
behind each one. Nothing in this package prints, logs or raises with a value.

The rules live in five families, one module each (F1 step 8): `basic` (the rules of the first release), `lexicon`
(the word lists), `proposed` (the rules of proposed mode and the rule sets), `learned` (the learned forms) and
`runstate` (the vocabulary, one run and the interface of the intake). They load in that order: a family imports the
names of the ones above it at its top and the names of the ones below it, which import it, at its end. This module
holds what they all read (`_list`, `_fold`, `skeleton`) and is their door: every name of a family is reached here
(awb/parts.py), and the old path `awb.wipe` is this module itself (awb/wipe.py).
"""
from __future__ import annotations

import inspect
import re
import unicodedata

from awb import normalize, parts, patterns
from awb import report as _awb_report


def _list(name: str, section: str | None = None, single: bool = False) -> frozenset[str]:
    """The entries of rules/<name>, case folded; with `section` only the lines after "# section: <section>" up to
    the next section line; with `single` only entries of one word. A missing file gives an empty set."""
    try:
        lines = (patterns.RULES_DIR / name).read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    out: set[str] = set()
    inside = section is None
    for line in lines:
        line = line.strip()
        if line.startswith("# section:"):
            inside = section is not None and line[len("# section:"):].strip() == section
            continue
        if not line or line.startswith("#") or not inside:
            continue
        if single and len(line.split()) != 1:
            continue
        out.add(line.casefold())
    return frozenset(out)


def _fold(word: str) -> str:
    """Case folded, accents dropped: the letters a reader would still read as the same word."""
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


_SKELETON_WORD_RE = re.compile(r"[^\W_]+")


def skeleton(text: str) -> str:
    """The letters and digits of `text`, normalised, folded and joined: the same for every way of writing it."""
    return "".join(_fold(w) for w in _SKELETON_WORD_RE.findall(normalize.normalize(text).text))


_REPORT_WORDS = frozenset(w.casefold() for w in re.findall(r"[A-Za-z]{4,}", inspect.getsource(_awb_report)))
"""Every word of the report module's source: a learned form may never be one of them, or the public report fails
its own check (Candidates, Files, Outputs, Notes)."""


# --------------------------------------------------------------------------- the families (F1 step 8)

from awb.intake.wipe import basic, lexicon, proposed, learned, runstate  # noqa: E402,F401 (loaded in this order)

parts.join(__name__, "awb.intake.wipe.basic", "awb.intake.wipe.lexicon", "awb.intake.wipe.proposed",
           "awb.intake.wipe.learned", "awb.intake.wipe.runstate", classes=True)
