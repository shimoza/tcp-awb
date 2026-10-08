"""Invented names for every test. Nothing here is real. No test may use any other name.

If a test needs a name that is not listed, add it here first. The self-clean test checks that every
capitalised multi-word string in the fixture files of the test suite comes from this list.
"""
from __future__ import annotations

from datetime import date

TODAY = date(2026, 9, 22).isoformat()

CUSTOMER_CODE = "CUST-Q7M4"
PARTNER_CODE = "PART-KX2A"
ORG_CODE = "ORG-B3NP"

# customer: full legal name, short form, acronym, English form, domain
CUSTOMER_FORMS = [
    "Zyxwo Logistik GmbH",
    "Zyxwo",
    "ZLG",
    "Zyxwo Logistics Ltd",
]
CUSTOMER_DOMAIN = "zyxwo-logistik.example"

PERSON_FORMS = ["Tobias Beispielmann", "Beispielmann"]
PERSON_CODE = CUSTOMER_CODE + "-PERS-1"

ORG_FORMS = ["Qvörtz Präzision AG", "Qvörtz"]
LAWFIRM_FORMS = ["Kanzlei Vbnmq & Partner mbB", "Vbnmq"]
LAWFIRM_CODE = "ORG-C4RT"

PLACE_FORMS = ["Beispielstadt"]
PLACE_CODE = CUSTOMER_CODE + "-SITE-1"

FILE_NUMBER = "26-0999"
TENDER_ID = "VG-2026-0457"

# a control name that is NOT registered and must never be replaced
CONTROL_UNREGISTERED = "Musterhausen"

# words that look like names but must stay (stop list controls)
KEEP_WORDS = ["Netzwerkbrücke", "Load Balancer", "Terraform", "PostgreSQL"]

ALL_REGISTERED = CUSTOMER_FORMS + PERSON_FORMS + ORG_FORMS + LAWFIRM_FORMS + PLACE_FORMS


def register_lines() -> list[str]:
    """The fixture register as TSV lines including the header."""
    rows = [("code", "kind", "form", "added", "status")]
    for f in CUSTOMER_FORMS:
        rows.append((CUSTOMER_CODE, "CUST", f, TODAY, "active"))
    rows.append((CUSTOMER_CODE + "-DOM-1", "DOM", CUSTOMER_DOMAIN, TODAY, "active"))
    for f in PERSON_FORMS:
        rows.append((PERSON_CODE, "PERS", f, TODAY, "active"))
    for f in ORG_FORMS:
        rows.append((ORG_CODE, "ORG", f, TODAY, "active"))
    for f in LAWFIRM_FORMS:
        rows.append((LAWFIRM_CODE, "ORG", f, TODAY, "active"))
    for f in PLACE_FORMS:
        rows.append((PLACE_CODE, "SITE", f, TODAY, "active"))
    rows.append((CUSTOMER_CODE + "-REF-1", "REF", FILE_NUMBER, TODAY, "active"))
    rows.append((CUSTOMER_CODE + "-REF-2", "REF", TENDER_ID, TODAY, "active"))
    # a retired form: must never be matched, its code must never be reused
    rows.append(("PART-RET2", "PART", "Altfirma Retired KG", TODAY, "retired"))
    return ["\t".join(r) for r in rows]


def assert_no_fixture_name(text: str, where: str = "") -> None:
    """Fail loudly (with the count only, never the value) if any registered fixture form is in `text`."""
    low = text.lower()
    hits = [f for f in ALL_REGISTERED + [CUSTOMER_DOMAIN, FILE_NUMBER, TENDER_ID] if f.lower() in low]
    assert not hits, "%d registered fixture forms found in %s" % (len(hits), where or "text")


# the invented form the gate selftest plants into its own temporary folder (awb/gate.py carries the same
# string); it is not in the fixture register above
SELFTEST_FORM = "Qwxzv Probefirma"

# the unregistered company the intake test plants; the intake must list it as a candidate and block
PLANTED_CANDIDATE = "Nrgtz Beratung GmbH"

# an unregistered invented person for the candidate rules (two capitalised words, a mail local part)
PLANTED_PERSON = "Xqarv Pomblet"

# the invented names of the wipe cases (T4, wipe mode); none of them is registered. Each one is built from single
# words, so that no capitalised run of two words stands in this file (tests/test_self_clean.py reads them)
SECOND_FIRST = "Ulvrad"
SECOND_LAST = "Qesmotz"
SECOND_PERSON = SECOND_FIRST + " " + SECOND_LAST
SECOND_COMPANY = " ".join(("Grvetz", "Systemhaus", "AG"))
CYRILLIC_PERSON = "Ксарв Помблет"
CYRILLIC_GENITIVE = "Помблета"
PLANTED_STREET = "Xqarvstraße 12"

# the self-test's own customer (awb/planted.py carries the same strings): its short form inside a company shape must
# become the code
SELFTEST_CUSTOMER_CODE = "CUST-SELF"
SELFTEST_CUSTOMER_SHORT = "Wqyzt"
SELFTEST_CUSTOMER_FORM = " ".join((SELFTEST_CUSTOMER_SHORT, "Spedition", "GmbH"))
