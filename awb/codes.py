"""One placeholder grammar for the whole Workbench.

Every replaced thing, whether a registered name or a piece of structured data, is written as

    <KIND>-<ID>              e.g. CUST-Q7M4, MAIL-3K7A
    <KIND>-<ID>-<KIND>-<n>   e.g. CUST-Q7M4-PERS-2 (a person that belongs to a customer)

ID is four characters from the base32 alphabet (A-Z and 2-7), generated at random. A code is never derived
from a name, a contact, a date or an order of arrival. Project codes use the same ID shape in lowercase
with the platform prefix: tcp-q7m4.
"""
from __future__ import annotations

import re
import secrets
import unicodedata

ENTITY_KINDS = ("CUST", "PART", "ORG", "PERS", "DOM", "SITE", "REF")
"""customer, partner, other organisation, person, domain or host name, place or address, file or case number"""

DATA_KINDS = ("MAIL", "URL", "IP", "MAC", "IBAN", "BIC", "VAT", "PHONE", "HRB", "TAX")
"""structured data found by pattern, replaced with the same grammar"""

KINDS = ENTITY_KINDS + DATA_KINDS
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

PLACEHOLDER_RE = re.compile(
    r"\b(?:%s)-[A-Z2-7]{4}(?:-(?:%s)-\d{1,3})?\b" % ("|".join(KINDS), "|".join(ENTITY_KINDS))
)
PROJECT_CODE_RE = re.compile(r"\b(?:tcp|hcs)-[a-z2-7]{4}\b")


def _id() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(4))


def new_code(kind: str, existing: set[str] | frozenset[str] = frozenset()) -> str:
    """A fresh code of the given kind that is not in `existing`."""
    if kind not in KINDS:
        raise ValueError("unknown kind: %s" % kind)
    for _ in range(1000):
        c = "%s-%s" % (kind, _id())
        if c not in existing:
            return c
    raise RuntimeError("could not find a free code")


def new_project_code(platform: str, existing: set[str] | frozenset[str] = frozenset()) -> str:
    if platform not in ("tcp", "hcs"):
        raise ValueError("unknown platform: %s" % platform)
    for _ in range(1000):
        c = "%s-%s" % (platform, _id().lower())
        if c not in existing:
            return c
    raise RuntimeError("could not find a free project code")


def sub_code(parent: str, kind: str, n: int) -> str:
    """CUST-Q7M4 + PERS + 2 -> CUST-Q7M4-PERS-2"""
    if not is_code(parent) or parent.count("-") != 1:
        raise ValueError("parent must be a top-level code")
    if kind not in ENTITY_KINDS:
        raise ValueError("unknown kind: %s" % kind)
    return "%s-%s-%d" % (parent, kind, n)


def is_code(s: str) -> bool:
    return bool(PLACEHOLDER_RE.fullmatch(s or ""))


def is_project_code(s: str) -> bool:
    return bool(PROJECT_CODE_RE.fullmatch(s or ""))


def kind_of(code: str) -> str:
    return code.split("-")[0]


def project_platform(folder) -> str:
    """The platform of a project folder by the prefix of its project code (the folder name): hcs for an hcs-
    project, tcp for every other folder. What a project writes into its own files cannot change it."""
    name = str(folder).rstrip("/").rsplit("/", 1)[-1]
    return "hcs" if is_project_code(name) and name.startswith("hcs-") else "tcp"


# Characters that read as a hyphen: the Unicode dashes (category Pd), the minus signs and the fullwidth and small
# forms. A code written with one of them is still the code (CUST<U+2011>Q7M4 reads as CUST-Q7M4).
_DASH_LIKE = re.compile(r"[\u2010-\u2015\u2043\u2212\u2e3a\u2e3b\ufe58\ufe63\uff0d\u058a\u05be\u1806\u2e17"
                        r"\u2e1a\u301c\u3030\u30a0\ufe31\ufe32]")


def fold_for_codes(text: str) -> str:
    """`text` the way a reader sees a code in it: NFKC (fullwidth letters and digits become ASCII), every
    invisible format character removed and every dash-like character read as an ASCII hyphen."""
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return _DASH_LIKE.sub("-", text)


def count_codes(text: str) -> tuple[int, int]:
    """(register codes, project codes) in `text`, also when a code is written with a look-alike dash, fullwidth
    characters or invisible characters inside."""
    folded = fold_for_codes(text)
    n_reg = max(len(PLACEHOLDER_RE.findall(text or "")), len(PLACEHOLDER_RE.findall(folded)))
    n_proj = max(len(PROJECT_CODE_RE.findall(text or "")), len(PROJECT_CODE_RE.findall(folded)))
    return n_reg, n_proj
