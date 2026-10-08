"""The red-team pack of calibration/redteam is sound and runs.

Every case module imports, every case carries the fields its harness needs and an id that is unique in its module,
every planted value is a fixture form, a string the module derives from one or a structured value the module builds
and declares (DERIVED, INVENTED), the invented values stay inside the documentation ranges of the brief, the
capitalised runs of the pack come from the fixtures, EXPECTED.md names cases that exist, and a small sample of every
dimension runs end to end through the harnesses with the outcome the pack records. The full runs are a manual step
(calibration/redteam/README.md); this file finishes in well under a minute.

Every name comes from tests/fixtures.py through the case modules. Nothing here prints a planted value.
"""
from __future__ import annotations

import html
import importlib
import re
import unicodedata
import urllib.parse
from pathlib import Path

import pytest

from awb import gate, patterns
from calibration.redteam import gate_harness, harness
from tests import fixtures as fx

REPO = Path(__file__).resolve().parent.parent
PACK = REPO / "calibration" / "redteam"
INTAKE = ("structured", "forms", "derived", "office", "pdf", "mail_archive", "nontext")
CLASSES = frozenset(("name", "mail", "url", "ip", "mac", "iban", "bic", "vat", "phone", "hrb", "tax", "other"))
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")

# three cases per intake dimension and five of the gate: cheap builds, with the outcome the pack records
SAMPLE = {
    "structured": (("iban-dashes", "caught"), ("phone-dots", "caught"), ("np-card", "escape")),
    "forms": (("b64-nopad-full", "caught"), ("leet-short-0", "escape"), ("qp-ascii-short", "escape")),
    "derived": (("d-compound-team", "caught"), ("d-typo-one-letter", "escape"), ("d-acr-glue-vm", "escape")),
    "office": (("docx-hidden-runs", "caught"), ("docx-table-caption", "unseen"), ("xlsx-numfmt", "unseen")),
    "pdf": (("pdf-plain-iban", "caught"), ("pdf-outline", "unseen"), ("pdf-js", "unseen")),
    "mail_archive": (("m-fold-iban-plain", "caught"), ("m-body-qp-softbreak", "caught"), ("a-7z", "unseen")),
    "nontext": (("sql-bytea-hex", "caught"), ("png-text", "unseen"), ("mp3-id3", "unseen")),
}
GATE_SAMPLE = (("s-flag-eq", "caught"), ("h-users-mac", "caught"), ("o-underscore", "caught"),
               ("i-uuid-after-dash", "caught"), ("n-rot13", "escape"))

# ordinary words that stand next to each other in carriers, XML attributes, CSV headers and date lines of the pack
PACK_WORDS = frozenset((
    "Steuer", "Nr", "Liberation", "Sans", "Verwaltungs", "Limited", "Inc", "Herrn", "Dr", "Last", "First", "Akte", "Az",
    "Relationship", "Id", "Default", "Extension", "Choice", "Requires", "Data", "Source", "Initial", "Catalog", "Text",
    "Box", "Tue", "Mon", "Sep", "User", "Name", "Access", "Key", "Secret", "Architect", "Workbench", "Team", "Mandant",
))
GENERIC_WORDS = frozenset(("GmbH", "Ltd", "Partner", "Kanzlei", "Logistik", "Logistics", "Präzision", "Beratung",
                           "Retired", "Altfirma"))
CAP_RUN = re.compile(r"(?<![\w-])[A-ZÄÖÜ][a-zäöüß]+(?:[ \t]+[A-ZÄÖÜ][a-zäöüß]+)+(?![\w-])")

DOC_NETS = ((203, 0, 113), (198, 51, 100), (192, 0, 2))
QUAD_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
IPV6_RE = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{1,4}:){2,}[0-9A-Fa-f:.]*")
MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
MAIL_RE = re.compile(r"[^\s@\"'<>()]+@([A-Za-z0-9.\[\]:_-]+)")
HOST_RE = re.compile(r"(?<![\w.-])(?:[a-z0-9_-]+\.)+[a-z]{2,}(?![\w-])")
TAX_DIGITS = frozenset(("1234567890", "12345678901", "1121081508150", "01381508153", "13381508159"))
EXAMPLE_BBAN = "370400440532013000"      # the documentation IBAN of awb/planted.py


@pytest.fixture(scope="module")
def modules():
    return {name: importlib.import_module("calibration.redteam." + name) for name in INTAKE + ("gate",)}


def cases_of(modules, name):
    return {c["id"]: c for c in modules[name].CASES}


# --------------------------------------------------------------------------- the shape of every case


@pytest.mark.parametrize("name", INTAKE)
def test_intake_cases_have_the_fields_the_harness_needs(modules, name):
    cases = modules[name].CASES
    assert len(cases) >= 50
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids), "%s: duplicate ids" % name
    for c in cases:
        assert ID_RE.fullmatch(c["id"]), "%s: id %r" % (name, c["id"])
        assert c["cls"] in CLASSES, "%s: %s has class %r" % (name, c["id"], c["cls"])
        assert isinstance(c.get("carrier"), str) and c["carrier"], "%s: %s has no carrier" % (name, c["id"])
        assert isinstance(c["values"], list) and all(isinstance(v, str) for v in c["values"]), "%s: %s values" % (name, c["id"])
        assert all(isinstance(v, str) for v in c.get("visible") or []), "%s: %s visible" % (name, c["id"])
        assert callable(c["build"]), "%s: %s has no build" % (name, c["id"])
        # a case without values and markers only proves that the run does not crash (a hand check case)


def test_gate_cases_have_the_fields_the_harness_needs(modules):
    cases = modules["gate"].CASES
    assert len(cases) >= 200
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids), "gate: duplicate ids"
    for c in cases:
        assert ID_RE.fullmatch(c["id"]), "gate: id %r" % c["id"]
        assert c["cls"] in gate.CLASSES, "gate: %s has class %r" % (c["id"], c["cls"])
        assert isinstance(c.get("carrier"), str) and c["carrier"], "gate: %s has no carrier" % c["id"]
        assert isinstance(c["path"], str) and c["path"] and not c["path"].startswith("/") and ".." not in c["path"]
        assert isinstance(c["content"], (str, bytes)), "gate: %s content" % c["id"]


# --------------------------------------------------------------------------- every value is invented


def fold(text: str) -> str:
    """A reader's skeleton of a value: markup, entities and percent escapes undone, the spelled-out at and dot of an
    obfuscated address folded back, then the harness skeleton (look-alikes folded, separators dropped)."""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"<[^<>]*>", "", t)
    t = urllib.parse.unquote(html.unescape(t))
    t = re.sub(r"(?i)[(\[]\s*at\s*[)\]]|\s+at\s+", "@", t)
    t = re.sub(r"(?i)[(\[]\s*dot\s*[)\]]|\s+dot\s+|\[\.\]", ".", t)
    return harness.skeleton(t)


def _fixture_stems() -> tuple[set[str], set[str]]:
    forms = fx.ALL_REGISTERED + [fx.CUSTOMER_DOMAIN, fx.FILE_NUMBER, fx.TENDER_ID, fx.PLANTED_CANDIDATE,
                                 fx.PLANTED_PERSON, fx.CONTROL_UNREGISTERED, fx.SELFTEST_FORM]
    whole = {harness.skeleton(f) for f in forms}
    words = {harness.skeleton(w) for f in forms for w in f.split() if len(w) > 3 and w not in GENERIC_WORDS}
    return whole, whole | words


FIXTURE_WHOLE, FIXTURE_STEMS = _fixture_stems()


def carries_fixture(text: str) -> bool:
    sk = fold(text)
    if any(stem in sk for stem in FIXTURE_STEMS):
        return True
    return len(sk) >= 4 and any(sk in whole for whole in FIXTURE_WHOLE)


def declared(text: str, mod) -> bool:
    stems = [s for s in mod.DERIVED + mod.INVENTED]
    if text in stems:
        return True
    sk = fold(text)
    for stem in stems:
        st = harness.skeleton(stem)
        if st and (st in sk or (len(sk) >= 3 and sk in st)):
            return True
    return False


def value_ok(text: str, mod) -> bool:
    if carries_fixture(text) or declared(text, mod):
        return True
    return any(carries_fixture(block) or declared(block, mod) for block in harness.decoded_blocks(text))


@pytest.mark.parametrize("name", INTAKE)
def test_every_value_is_a_fixture_form_or_declared_by_its_module(modules, name):
    mod = modules[name]
    assert isinstance(mod.DERIVED, tuple) and isinstance(mod.INVENTED, tuple)
    assert all(isinstance(s, str) and s for s in mod.DERIVED + mod.INVENTED)
    bad = [c["id"] for c in mod.CASES for v in c["values"] if not value_ok(v, mod)]
    assert not bad, "%s: values outside the fixtures and the declarations in %s" % (name, ", ".join(sorted(set(bad))))
    # a marker is innocent prose; a marker that is exactly a registered form is the name as a reader sees it (the
    # font remap case), anything between is a value in disguise
    marks = [c["id"] for c in mod.CASES for m in c.get("visible") or []
             if carries_fixture(m) and fold(m) not in FIXTURE_WHOLE]
    assert not marks, "%s: an innocent marker carries a fixture form in %s" % (name, ", ".join(sorted(set(marks))))


def test_the_value_check_can_fail(modules):
    mod = modules["structured"]
    probe = "%s@%s" % ("qwxzv".replace("q", "b"), ".".join(("vbnkq" + "tail", "de")))
    assert not value_ok(probe, mod)
    assert value_ok(fx.CUSTOMER_FORMS[1].lower() + "-backup01", mod)
    assert value_ok(mod.INVENTED[0], mod)


def _hosts_ok(text: str, labels: tuple) -> list[str]:
    bad = []
    low = unicodedata.normalize("NFKC", text).lower()
    for m in MAIL_RE.finditer(low):
        host = m.group(1)
        if "example" not in host and not host.startswith("[ipv6:2001:db8"):
            bad.append("mail host")
    for m in HOST_RE.finditer(low):
        host = m.group(0)
        if QUAD_RE.fullmatch(host) or "example" in host or host.endswith((".invalid", ".test")):
            continue
        if not any(host.startswith(label + ".") for label in labels):
            bad.append("host")
    return bad


def _addresses_ok(text: str) -> list[str]:
    bad = []
    t = unicodedata.normalize("NFKC", text)
    for m in QUAD_RE.finditer(t):
        if tuple(int(g) for g in m.groups()[:3]) not in DOC_NETS:
            bad.append("ipv4")
    for m in IPV6_RE.finditer(t):
        token = m.group(0)
        if MAC_RE.fullmatch(token):
            continue
        if not token.lower().startswith(("2001:db8", "2001:0db8", "::ffff:")):
            bad.append("ipv6")
    return bad


def _digit_families_ok(text: str) -> list[str]:
    bad = []
    for span in patterns.find_structured(text):
        piece = text[span.start:span.end]
        digits = re.sub(r"\D", "", piece)
        if span.cls == "iban":
            bban = re.sub(r"[^A-Za-z0-9]", "", piece).upper()[4:]
            if bban != EXAMPLE_BBAN and not re.fullmatch(r"[50]+\d{2}", bban):
                bad.append("iban")
        elif span.cls == "vat":
            if not "12345678901".startswith(digits) and digits != "123456789001":
                bad.append("vat")
        elif span.cls in ("phone", "hrb", "tax"):
            if re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,4}){3}", piece):
                continue        # a dotted address form (octal, decimal or zero padded), judged by the quad rule
            if "555" not in digits and "1234" not in digits and digits not in TAX_DIGITS:
                bad.append(span.cls)
    return bad


@pytest.mark.parametrize("name", INTAKE)
def test_invented_values_stay_inside_the_documentation_ranges(modules, name):
    mod = modules[name]
    labels = tuple(getattr(mod, "INVENTED_LABELS", ()))
    bad = {}
    for i, s in enumerate(mod.INVENTED):
        problems = _hosts_ok(s, labels) + _addresses_ok(s) + _digit_families_ok(s)
        if problems:
            bad[i] = sorted(set(problems))
    assert not bad, "%s: INVENTED entries outside the brief's ranges (index: kinds): %s" % (name, bad)


def test_the_range_check_can_fail():
    assert _addresses_ok("host 198.51.100.7 and 2001:db8::1 fine") == []
    assert _addresses_ok("host " + ".".join(("10", "20", "30", "40"))) == ["ipv4"]
    assert _addresses_ok("2001:" + "0db9::1")
    assert _hosts_ok("write to a@b.example", ()) == []
    assert _hosts_ok("write to a@" + "b.org", ()) == ["mail host", "host"]
    assert _hosts_ok("see qwxzv-portal.shop", ("qwxzv-portal",)) == []
    assert _hosts_ok("see qwxzv-portal.shop", ()) == ["host"]
    assert _digit_families_ok("call 030 5551234") == []
    assert _digit_families_ok("call 030 " + "9876543") == ["phone"]


# --------------------------------------------------------------------------- the source of the pack


def test_capitalised_runs_of_the_pack_come_from_the_fixtures():
    allowed = FIXTURE_STEMS_WORDS | PACK_WORDS
    bad = []
    seen = 0
    for path in sorted(PACK.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in CAP_RUN.finditer(text):
            seen += 1
            if any(w not in allowed for w in m.group(0).split()):
                bad.append("calibration/redteam/%s:%d" % (path.name, text.count("\n", 0, m.start()) + 1))
    assert seen > 0
    assert not bad, "capitalised runs with words that are not from the fixtures:\n" + "\n".join(bad)


def _fixture_words() -> set[str]:
    strings = (fx.ALL_REGISTERED + fx.KEEP_WORDS + fx.register_lines()
               + [fx.CONTROL_UNREGISTERED, fx.PLANTED_CANDIDATE, fx.PLANTED_PERSON, fx.SELFTEST_FORM])
    words = {w for s in strings for w in re.findall(r"[^\W\d_]+", s)}
    for line in (REPO / "rules" / "stop-words.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            words.update(line.split())
    return words


FIXTURE_STEMS_WORDS = _fixture_words()


def test_no_case_module_spells_a_fixture_form(modules):
    """The modules reference tests/fixtures.py; the source text itself carries no registered form."""
    forms = [f.lower() for f in fx.ALL_REGISTERED + [fx.CUSTOMER_DOMAIN, fx.FILE_NUMBER, fx.TENDER_ID]
             if len(f) > 3]
    bad = []
    for path in sorted(PACK.glob("*.py")):
        low = path.read_text(encoding="utf-8").lower()
        for f in forms:
            if f in low:
                bad.append("calibration/redteam/%s carries fixture form %d" % (path.name, forms.index(f)))
    assert not bad, "\n".join(bad)


def test_expected_ids_reads_one_section_at_a_time(tmp_path):
    f = tmp_path / "EXPECTED.md"
    f.write_text("# x\n\n## alpha\n\n- one: reason\n- two: reason, more\n\nClipped:\n\n- three: clipped\n\n"
                 "## beta\n\n- four: reason\n", encoding="utf-8")
    assert harness.expected_ids(f, "alpha") == {"one", "two", "three"}
    assert harness.expected_ids(f, "beta") == {"four"}
    assert harness.expected_ids(f, "gamma") == set()
    assert gate_harness.expected_ids(f, "beta") == {"four"}


def test_expected_leftovers_name_existing_cases(modules):
    path = PACK / "EXPECTED.md"
    text = path.read_text(encoding="utf-8")
    for name in INTAKE + ("gate",):
        assert "\n## %s" % name in text, "EXPECTED.md has no section for %s" % name
        listed = harness.expected_ids(path, name)
        assert listed, "EXPECTED.md lists nothing under %s" % name
        unknown = listed - set(cases_of(modules, name))
        assert not unknown, "EXPECTED.md names cases %s does not have: %s" % (name, ", ".join(sorted(unknown)))


# --------------------------------------------------------------------------- a sample runs end to end


def outcome(entry: dict) -> str:
    if entry["error"]:
        return "error"
    if entry["escape"]:
        return "escape"
    if entry["unseen"]:
        return "unseen"
    return "caught"


@pytest.mark.parametrize("name", INTAKE)
def test_a_sample_of_every_intake_dimension_runs(modules, name, tmp_path):
    cases = cases_of(modules, name)
    harness.code_under_test()
    for cid, want in SAMPLE[name]:
        entry = harness.run_case(cases[cid], tmp_path)
        assert entry["error"] is None, "%s/%s: %s" % (name, cid, entry["error"])
        assert outcome(entry) == want, "%s/%s: %s, expected %s" % (name, cid, outcome(entry), want)
        assert entry["unforced"]["outputs"] >= 0 and "workbench" not in entry["unforced"]


def test_a_sample_of_the_gate_cases_runs(modules, tmp_path):
    cases = cases_of(modules, "gate")
    reg = gate_harness.fixture_register(tmp_path)
    for i, (cid, want) in enumerate(GATE_SAMPLE):
        entry = gate_harness.scan_case(cases[cid], reg, tmp_path / ("c%d" % i))
        assert entry["error"] is None
        got = "escape" if entry["escape"] else "caught"
        assert got == want, "gate/%s: %s, expected %s (found %s)" % (cid, got, want, ",".join(entry["found"]))


def test_the_harness_restores_the_environment(modules, tmp_path, monkeypatch):
    monkeypatch.setenv("AWB_SHARED", str(tmp_path / "before"))
    monkeypatch.delenv("AWB_VAULT", raising=False)
    harness.run_case(cases_of(modules, "structured")["iban-dashes"], tmp_path)
    import os
    assert os.environ["AWB_SHARED"] == str(tmp_path / "before")
    assert "AWB_VAULT" not in os.environ


# --------------------------------------------------------------------------- the wipe pack (T4, 2026-10-08)

from calibration.redteam.wipe import harness as wipe_harness  # noqa: E402

WIPE_SAMPLE = {
    "transcripts": (("teams-seconds", "clean"), ("speech-nickname", "clean")),
    "mail_chains": (("ics-folded-name", "clean"), ("hdr-wrapped-list", "clean")),
    "decks": (("pptx-table-services", "clean"), ("pptx-cover-erstellt-von", "clean")),
    "code_files": (("tf-comment-lower-surname", "leak"), ("tf-ident-known-first-suffix-surname", "clean"),
                   ("csv-inventory-owner-lower", "clean")),
    "tables": (("md-host-number-before-ip-cell", "clean"), ("docx-keyvalue-team-values", "loss")),
    "german": (("de-lone-after-preposition", "leak"), ("de-particle-known-first", "clean")),
    "russian": (("ru-customer-translit-short-alone", "leak"), ("ru-title-gn-gzha-prose", "clean")),
    "disguised": (("pdf-two-columns-first-last", "clean"), ("md-bold-each-word", "clean")),
    "losses": (("fp-title-dr", "clean"), ("fp-learned-report-word", "clean")),
}


@pytest.fixture(scope="module")
def wipe_cases():
    return {dim: {c["id"]: c for c in wipe_harness.load_cases(wipe_harness.module_path(dim))}
            for dim in wipe_harness.DIMENSIONS}


def test_the_wipe_pack_holds_nine_dimensions_of_45_cases(wipe_cases):
    assert sorted(wipe_cases) == sorted(WIPE_SAMPLE)
    assert all(len(cases) == 45 for cases in wipe_cases.values())
    for cases in wipe_cases.values():
        for c in cases.values():
            assert {"id", "values", "keep", "build"} <= set(c) and ID_RE.fullmatch(c["id"])


def test_expected_lists_every_wipe_dimension_and_only_cases_that_exist(wipe_cases):
    path = PACK / "EXPECTED.md"
    for dim, cases in wipe_cases.items():
        section = "wipe-" + dim.replace("_", "-")
        assert "\n## %s\n" % section in path.read_text(encoding="utf-8"), section
        listed = harness.expected_ids(path, section) - {"none"}
        assert listed <= set(cases), "EXPECTED.md names cases %s does not have" % section


def test_the_wipe_harness_reports_a_planted_leak_and_a_forced_loss_first(tmp_path):
    assert wipe_harness.selfcheck(tmp_path) == []


@pytest.mark.parametrize("dim", sorted(WIPE_SAMPLE))
def test_a_sample_of_every_wipe_dimension_runs_with_the_recorded_outcome(dim, wipe_cases, tmp_path):
    listed = harness.expected_ids(PACK / "EXPECTED.md", "wipe-" + dim.replace("_", "-"))
    for cid, want in WIPE_SAMPLE[dim]:
        entry = wipe_harness.run_case(wipe_cases[dim][cid], tmp_path)
        assert wipe_harness.outcome(entry) == want, cid
        assert (want != "clean") == (cid in listed), "%s: EXPECTED.md and the sample disagree" % cid


def test_the_false_positive_dimension_loses_no_term_and_leaks_no_name(wipe_cases, tmp_path):
    bad = [cid for cid, case in wipe_cases["losses"].items()
           if wipe_harness.outcome(wipe_harness.run_case(case, tmp_path)) != "clean"]
    assert bad == []
