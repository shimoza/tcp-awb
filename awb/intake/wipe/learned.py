"""The learned forms: the forms of every wiped person and company, matched over every output of the run like a
registered form (`_Learned`), and the host and domain labels they come from. One family of the wipe rules (F1 step
8); the package text is awb/intake/wipe/__init__.py.
"""
from __future__ import annotations

import re

from awb import codes, patterns, register
from awb.intake.wipe import _REPORT_WORDS, skeleton
from awb.intake.wipe.lexicon import (_FIRST_NAMES, _GENERIC_ADDRESSEES, _GENERIC_COMPANY_WORDS, _GERMAN_ADJ,
                                     _GERMAN_FUNCTION, _GERMAN_KNOWN, _NEUTRAL_LABELS, _PARTICLE_WORDS, _TITLE_WORDS,
                                     _known_word, _letters, _person_known)
from awb.intake.wipe.proposed import _SYSTEM_USERS, _name_shaped
from awb.matcher import GLUE_AFFIXES, Matcher, Span


# --------------------------------------------------------------------------- learned forms (proposed)

_PUBLIC_PROVIDERS = frozenset((
    "gmail", "googlemail", "outlook", "hotmail", "live", "msn", "yahoo", "ymail", "icloud", "me", "mac", "aol",
    "gmx", "web", "t-online", "tonline", "freenet", "arcor", "posteo", "mailbox", "protonmail", "proton", "tutanota",
    "yandex", "mail", "rambler", "bk", "list", "inbox", "example", "test", "localhost", "invalid", "github", "gitlab",
    "bitbucket", "microsoft", "google", "amazon", "apple", "facebook", "linkedin", "twitter", "xing", "slack",
    "zoom", "teams", "webex", "atlassian", "notion", "dropbox", "box", "wetransfer", "telekom", "t-systems",
    "otc", "open-telekom-cloud", "opentelekomcloud", "huaweicloud", "huawei", "docs", "wiki", "www", "t-cloud",
    "tcloud", "t-systems", "tsystems", "telekom", "magentacloud", "cloudflare", "cloudflareaccess",
))


def _host_labels(value: str) -> list[str]:
    """The other labels of a host (srv-pomblet-01.nrgtz.example: pomblet), split at dots and hyphens, without
    digits, affixes, known words and short pieces: a surname in a host name."""
    host = value
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = re.sub(r"^[a-z]+://", "", host, flags=re.I).split("/")[0].split(":")[0].strip("[]<>.").casefold()
    labels = [l for l in host.split(".") if l]
    if len(labels) < 3 or patterns._allowed_host(host) or labels[-1] in patterns._FILE_SUFFIX_TLDS:
        return []
    out = []
    from awb.matcher import GLUE_AFFIXES
    for label in labels[:-2]:
        for piece in re.split(r"[-_]", label):
            if (_letters(piece) >= 4 and piece.isalpha() and piece not in GLUE_AFFIXES and piece not in _PUBLIC_PROVIDERS
                    and not _known_word(piece) and piece not in _SYSTEM_USERS):
                out.append(piece)
    return out


def _domain_label(value: str) -> str | None:
    """The second-level label of the host of a mail address or URL when it is not a known word, a public
    provider or a platform host; the label without its hyphens is what a company is called in identifiers."""
    host = value
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = re.sub(r"^[a-z]+://", "", host, flags=re.I).split("/")[0].split(":")[0].strip("[]<>.")
    labels = [l for l in re.split(r"\.", host.casefold()) if l]
    if len(labels) < 2 or patterns._allowed_host(host) or labels[-1] in patterns._FILE_SUFFIX_TLDS:
        return None   # the platform's own hosts, the documentation hosts and file names name no customer
    # the label before the public suffix (two labels for co.uk, com.au and the like)
    idx = -2
    if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac", "gov", "edu") and len(labels[-1]) == 2:
        idx = -3
    label = labels[idx]
    if label in _PUBLIC_PROVIDERS or _letters(label) < 4 or label.startswith("xn--") or "_" in label:
        return None
    if _known_word(label) or all(_known_word(p) or len(p) < 3 for p in label.split("-")):
        return None
    return label


_LATIN_ENDINGS = ("s", "es", "n", "en", "ns", "'s", "’s", "sche", "schen", "scher", "sches", "'sche", "’sche", "in")
_CYRILLIC_RE = re.compile(r"^[А-ЯЁа-яё-]+$")
_LOWER_WORD_BOUND = r"(?<![\w.@/-])%s(?![\w.@/-])"


class _Learned:
    """The run-local register: words of wiped persons and companies, matched like registered forms."""

    def __init__(self):
        self.words: dict[str, tuple[str, tuple[str, str]]] = {}   # casefold word -> (word as seen, token key)
        self.strong: dict[str, bool] = {}   # learned from a person or company shape, never a common word
        self.caps_only: dict[str, bool] = {}   # a dictionary word that is a surname: only when capitalised
        self.version = 0
        self.matcher: Matcher | None = None
        self.short_res: list[tuple[re.Pattern, tuple[str, str], bool]] = []
        self.cyr_res: list[tuple[re.Pattern, tuple[str, str]]] = []
        self.built = -1
        self.texts: list[str] = []

    def learn(self, cls: str, value: str, key: tuple[str, str], unknown_only: bool = False) -> None:
        strong = cls in ("person", "company")
        for raw in re.split(r"[\s/&+]+", value):
            for part in {raw} | set(re.split(r"[-'’.]", raw)):
                part = part.strip(".,;:!?()[]\"'«»„“”`")
                if _letters(part) < 3 or not part[0].isalpha() or not _name_shaped(part):
                    continue
                if part.isupper() and (len(part) <= 3 or cls != "person" and len(part) <= 5) and not value.isupper():
                    continue   # an acronym is no company word; a surname in capitals (ZRBL, QWERTZUIO) is learned
                if codes.is_code(part) or codes.is_project_code(part):
                    continue
                if "-" in part and all(_known_word(x) or _letters(x) < 3 for x in part.split("-")):
                    continue   # T-Systems, t-cloud: known words with a hyphen
                low = part.casefold()
                if low in self.words:
                    continue
                if low in _TITLE_WORDS or low in _PARTICLE_WORDS or low in _NEUTRAL_LABELS or low in _GENERIC_ADDRESSEES:
                    continue
                if low in _REPORT_WORDS or low in _runstate._RUN_EXEMPT or low in _GERMAN_FUNCTION:
                    continue   # the words of the reports' own text and the values under product labels are never learned
                caps_only = False
                if cls == "company" and _known_word(part, suffix=False):
                    continue
                if cls != "company" and _person_known(part):
                    if cls == "person" and part[:1].isupper() and not unknown_only and len(value.split()) <= 2:
                        caps_only = True   # Herr Lang, Frau Klein: learned, matched capitalised only
                    else:
                        continue
                if unknown_only and (part[:1].islower() or _letters(part) < 4):
                    continue
                if cls == "company" and low in _GENERIC_COMPANY_WORDS:
                    continue
                self.words[low] = (part, key)
                self.strong[low] = strong
                self.caps_only[low] = caps_only
                self.version += 1

    def _common(self, low: str) -> bool:
        """A Latin word that stands in lower case somewhere in the run's prose is a common word, not a name: not
        a first name of the list, not a word learned in lower case (a chat nick), and not an occurrence after a
        greeting or thanks ("danke tobias") or before a colon (a chat label)."""
        if _CYRILLIC_RE.match(low) or low in _FIRST_NAMES:
            return False
        if self.words.get(low, ("",))[0] == low:
            return False
        rx = re.compile(_LOWER_WORD_BOUND % re.escape(low))
        for t in self.texts:
            for m in rx.finditer(t):
                before = t[max(0, m.start() - 12):m.start()].casefold()
                if re.search(r"(?:hallo|hi|hey|danke|thanks|thx|dear|liebe|lieber|moin|servus|cc|@|an|to|привет|спасибо)[ ,]*$", before):
                    continue
                if t[m.end():m.end() + 1] == ":":
                    continue
                if m.start() >= 2 and t[m.start() - 1] == "(" and t[m.start() - 2].isalnum() and t[m.end():m.end() + 1] == ")":
                    continue   # TODO(pomblet), owner(pomblet): a tag argument, not prose
                return True
        return False

    def build(self) -> None:
        if self.built == self.version:
            return
        self.built = self.version
        pairs: list[tuple[str, str]] = []
        self.short_res, self.cyr_res = [], []
        for low, (word, key) in self.words.items():
            if self._common(low):
                if not self.strong.get(low):
                    continue
                if _known_word(word, suffix=False) or low in _CORPUS or low in _GERMAN_ADJ:
                    self.caps_only[low] = True   # Frau Klein is learned; "zu klein" in the same text stays
                # an unknown strong word (a surname no dictionary knows) matches in any case: "ask pomblet first"
            code = "L:%s:%s" % key
            if _CYRILLIC_RE.match(word):
                low = word.casefold()
                if low.endswith(("ский", "цкий", "ская", "цкая", "ской", "цкой", "ского", "цкого")):
                    stem = word[:-4] if low.endswith(("ского", "цкого")) else word[:-3]
                    ending = r"[а-яё]{2,4}"
                elif len(word) >= 4 and low[-1] in "аеиоуыэюяй":
                    stem = word[:-2] if low.endswith("ия") else word[:-1]
                    ending = r"[а-яё]{0,3}"
                else:
                    stem, ending = word, r"[а-яё]{0,3}"
                if len(stem) >= 3:
                    self.cyr_res.append((re.compile(r"(?<![^\W\d_])%s%s(?![^\W\d_])" % (re.escape(stem), ending),
                                         re.IGNORECASE), key))
                continue
            if _letters(word) >= 5:
                for v in register._variants(word):
                    pairs.append((v, code))
                for ending in _LATIN_ENDINGS:
                    pairs.append((word + ending, code))
            else:
                rx = re.compile(r"(?<![^\W\d])(?P<initial>[a-z])?(?:%s)(?:s|'s|’s)?(?![^\W\d])" % re.escape(word),
                                re.IGNORECASE)
                self.short_res.append((rx, key, word[:1].islower()))
        self.matcher = Matcher(pairs) if pairs else None

    def find(self, text: str, taken, keep=()) -> list[Span]:
        """Every learned form in `text` that `taken` leaves free and no phrase of `keep` covers."""
        self.build()
        if keep:
            kept = [(m.start(), m.end()) for k in keep if keep_key(k)
                    for m in re.finditer(r"(?<![\w])" + r"\s+".join(re.escape(w) for w in keep_key(k).split())
                                         + r"(?![\w])", text, re.IGNORECASE)]
            if kept:
                inner = taken

                def taken(a: int, b: int) -> bool:
                    return inner(a, b) or any(x <= a and b <= y for x, y in kept)
        out: list[Span] = []
        if self.matcher is not None:
            for s in self.matcher.find(text):
                key = s.code.split(":", 2)[1:]
                a, b = s.start, s.end
                matched = text[a:b]
                low_key = re.sub(r"[^\w]", "", matched.casefold())
                if any(self.caps_only.get(w) for w in (low_key, low_key.rstrip("s"))) and not matched[:1].isupper():
                    continue   # klein the adjective stays, Klein the surname goes
                letters = sum(c.isalpha() for c in matched)
                letter_spaced = letters >= 5 and re.fullmatch(r"(?:[^\W\d_][ \t.·-]{1,4}){4,}[^\W\d_]", matched)
                strong = any(self.strong.get(w) for w in (low_key, low_key.rstrip("s")))
                if not re.fullmatch(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", matched) and letters < 8 \
                        and not (letter_spaced and strong):
                    continue   # spread out or broken by marks: only a long or a strong learned form is read that way
                left = text[a - 1] if a > 0 else " "
                right = text[b] if b < len(text) else " "
                if left.isalnum() or right.isalnum():
                    # glued inside a token: an identifier (a digit, a separator or a case change in it) or a long
                    # word; never a short word inside a plain word
                    i, j = a, b
                    while i > 0 and text[i - 1].isalnum():
                        i -= 1
                    while j < len(text) and text[j].isalnum():
                        j += 1
                    token = text[i:j]
                    around = text[max(0, i - 1):j + 1]
                    identifier = (any(c.isdigit() for c in token) or re.search(r"[a-z][A-Z]", token)
                                  or any(c in "_.-@/" for c in around))
                    initial = (i == a - 1 and j == b)   # one letter before the surname: a login (tzrbko)
                    rest = (text[i:a] + text[b:j]).casefold()
                    compound = len(rest) >= 3 and (_known_word(rest) or rest in _GERMAN_KNOWN)   # Nrgtzlösung
                    if not identifier and not initial and not compound and (b - a) < 7:
                        continue
                if not taken(a, b):
                    out.append(Span(a, b, key[0], "L:%s:%s" % (key[0], key[1])))
        for rx, key, any_case in self.short_res:
            for m in rx.finditer(text):
                initial = bool(m.group("initial"))
                if not any_case and not initial and not m.group(0)[:1].isupper():
                    # inside an identifier (a hyphen, an underscore or a digit next to it) any case counts
                    around = text[max(0, m.start() - 1):m.end() + 1]
                    if not re.search(r"[-_\d]", around):
                        continue
                if initial and m.group(0)[:1].isupper() and m.group(0)[1:2].islower():
                    continue   # "Azrbl" capitalised with an initial is another word, not initial plus surname
                if not taken(m.start(), m.end()):
                    out.append(Span(m.start(), m.end(), key[0], "L:%s:%s" % key))
        for rx, key in self.cyr_res:
            for m in rx.finditer(text):
                if not taken(m.start(), m.end()):
                    out.append(Span(m.start(), m.end(), key[0], "L:%s:%s" % key))
        return out

    def skeletons(self) -> set[str]:
        return {skeleton(w) for w, _ in self.words.values() if _letters(w) >= 5 and not self._common(w.casefold())}


_RU_TITLES = frozenset(("г-н", "г-на", "г-ну", "г-ном", "г-не", "г-жа", "г-жи", "г-же", "г-жу", "г-жой", "господин",
                        "господина", "господину", "господином", "господине", "господа", "госпожа", "госпожи", "госпоже",
                        "госпожу", "госпожой", "уважаемый", "уважаемая", "уважаемые", "уважаемого", "уважаемому",
                        "уважаемым", "уважаемой", "уважаемую", "товарищ", "товарища", "тов", "коллега", "коллеге"))
_TITLES_EXTRA = _RU_TITLES | frozenset(("ing", "mag", "dipl-ing", "mx", "sir", "hr", "hrn",
                           "fr", "frl", "fam", "herren", "damen", "med", "rer", "nat", "jur", "phil", "oec", "pol",
                           "habil", "kfm", "kffr", "inform", "wirt", "päd", "theol", "vet", "dent", "pharm", "psych",
                           "ba", "ma", "msc", "bsc", "mba", "llm", "phd", "dipl-kfm", "dipl-inf", "dipl-wirt"))


# the names of the later families this one calls; they import this module, so these come after its own
from awb.intake.wipe.runstate import _CORPUS, keep_key  # noqa: E402
from awb.intake.wipe import runstate as _runstate  # noqa: E402  (bind rebinds _RUN_EXEMPT there)
