"""One definition of the code patterns (B-020, step 2 of the F1 plan): awb/codes.py spells the project, customer, fact
and calculation codes once and every other module of the package builds on that text."""
import re
from pathlib import Path

from awb import calc, codes, grants, intake_counts, kb, questions, review
from awb.tcp import keys, refresh, sweep, tenants, xchg
from awb.tcp.web import chat_service, contract, create_api, materials_api, projects_api

PACKAGE = Path(__file__).resolve().parents[1] / "awb"

# A local spelling: a character class written after a code prefix, or the four characters of a project code.
LOCAL = re.compile(r"(?:CUST|KB|K)-\[|(?:tcp|hcs)\)?-\[|\{3\}-\[|\[a-z2-7\]\{4\}")

# The gateway runs as a script under the system python and imports no module of awb (its docstring says so). It may
# keep its own spellings, but only the ones codes.py names.
STANDALONE = {"tcp/web/gateway.py": (codes.TCP_PROJECT, codes.TCP_PROJECT_WIDE)}


def test_central_patterns_accept_the_codes_and_refuse_the_rest():
    assert codes.PROJECT_RE.fullmatch("tcp-q7m4") and codes.PROJECT_RE.fullmatch("hcs-q7m4")
    for bad in ("tcp-q7m0", "tcp-Q7M4", "abc-q7m4", "tcp-q7m"):
        assert not codes.PROJECT_RE.fullmatch(bad), bad
    assert codes.CUST_RE.fullmatch("CUST-Q7M4")
    for bad in ("CUST-Q7M0", "CUST-q7m4", "PART-Q7M4", "CUST-Q7M4X"):
        assert not codes.CUST_RE.fullmatch(bad), bad
    assert codes.KB_RE.fullmatch("KB-Q7M4")
    for bad in ("KB-Q7M0", "KB-Q7M", "KB-Q7M4A"):
        assert not codes.KB_RE.fullmatch(bad), bad
    assert codes.CALC_RE.fullmatch("K-1") and codes.CALC_RE.fullmatch("K-99999")
    for bad in ("K-0", "K-01", "K-100000", "K-"):
        assert not codes.CALC_RE.fullmatch(bad), bad
    assert codes.PROJECT_CODE_RE is codes.PROJECT_RE
    assert codes.PROJECT_RE.findall("in tcp-q7m4 and CUST-Q7M4") == ["tcp-q7m4"]


def test_variants_keep_what_the_local_patterns_accepted():
    assert re.fullmatch(codes.TCP_PROJECT, "tcp-q7m4") and not re.fullmatch(codes.TCP_PROJECT, "hcs-q7m4")
    assert re.fullmatch(codes.TCP_PROJECT_WIDE, "tcp-q7m0") and not re.fullmatch(codes.TCP_PROJECT_WIDE, "hcs-q7m4")
    assert re.fullmatch(codes.ANY_PROJECT, "abc-q7m4") and not re.fullmatch(codes.ANY_PROJECT, "abc-q7m0")
    assert re.fullmatch(codes.KB_WIDE, "KB-Q7M0") and not re.fullmatch(codes.KB_WIDE, "KB-Q7M40")
    assert re.fullmatch(codes.KB_ANY_LENGTH, "KB-Q7M40") and not re.fullmatch(codes.KB_ANY_LENGTH, "KB-q7m4")
    assert re.fullmatch(codes.CALC_WIDE, "K-0") and re.fullmatch(codes.CALC_WIDE, "K-123456")


def test_the_modules_compile_the_same_patterns_as_before():
    """The text of every pattern that moved is the text the module wrote before (nothing widened, nothing narrowed)."""
    same = {
        questions._PROJECT_RE: r"tcp-[a-z0-9]{4}",
        grants.CODE_RE: r"^tcp-[a-z0-9]{4}$",
        grants.CUST_RE: r"^CUST-[A-Z2-7]{4}$",
        intake_counts.CUSTOMER_RE: r"^CUST-[A-Z2-7]{4}$",
        kb.ID_RE: r"KB-[A-Z2-7]{4}",
        kb._FILE_RE: r"KB-[A-Z2-7]{4}\.md",
        review._KB_EVIDENCE_RE: r"kb:(KB-[A-Z0-9]{4})",
        review._CALC_EVIDENCE_RE: r"calc:(K-[1-9][0-9]{0,4})",
        calc.ID_RE: r"K-[1-9][0-9]{0,4}",
        refresh._KB_ID_RE: r"\bKB-[A-Z2-7]{4}\b",
        keys._PROJECT_RE: r"^tcp-[a-z0-9]{4}$",
        sweep._CODE_IN_NAME_RE: r"(?<![a-z0-9])(?:tcp|hcs)-[a-z2-7]{4}(?![a-z0-9])",
        sweep._LAB_NAME_RE: r"^(?P<code>(?:tcp|hcs)-[a-z2-7]{4})-lab-(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})-[a-z]{4}"
                            r"(?:-[a-z0-9]{1,8})?$",
        xchg._CODE_RE: r"^tcp-[a-z0-9]{4}$",
        xchg._CUST_RE: r"^CUST-[A-Z2-7]{4}$",
        xchg._WEB_FOLDER_RE: r"^\d{4}-(?:0[1-9]|1[0-2])/(tcp-[a-z2-7]{4})/in/",
        tenants._PROJECT_TAG_RE: r"tcp-[a-z0-9]{4}",
        tenants._CODE_RE: r"^tcp-[a-z0-9]{4}$",
        projects_api.CODE: r"tcp-[a-z0-9]{4}",
        chat_service.CODE: r"tcp-[a-z0-9]{4}",
        chat_service.CHAT_ROUTE: r"/api/projects/(tcp-[a-z0-9]{4})/chat",
        create_api.CUSTOMER: r"CUST-[A-Z2-7]{4}",
        materials_api.CODE: r"tcp-[a-z2-7]{4}",
    }
    for compiled, before in same.items():
        assert compiled.pattern == before, before
    assert contract.PROJECT_CODE == r"^tcp-[a-z2-7]{4}$"
    assert contract.CUSTOMER_CODE == r"^CUST-[A-Z2-7]{4}$"


def test_no_module_spells_a_code_pattern_of_its_own():
    """Grep the package: only codes.py writes the character classes of a project, customer, fact or calculation code."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel == "codes.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not LOCAL.search(line):
                continue
            allowed = STANDALONE.get(rel, ())
            rest = line
            for text in allowed:
                rest = rest.replace(text, "")
            if LOCAL.search(rest):
                found.append("awb/%s:%d" % (rel, n))
    assert found == [], "spell the pattern with awb.codes: " + ", ".join(found)


def test_the_grep_finds_a_local_spelling():
    for line in ('X = re.compile(r"^tcp-[a-z0-9]{4}$")', 'Y = r"CUST-[A-Z2-7]{4}"', 'Z = r"kb:(KB-[A-Z0-9]{4})"',
                 'W = r"calc:(K-[0-9]+)"', 'V = r"(?:tcp|hcs)-[a-z2-7]{4}"', 'U = r"[a-z]{3}-[a-z2-7]{4}"',
                 'T = r"%s-[a-z2-7]{4}" % PLATFORM'):
        assert LOCAL.search(line), line
    for line in ('X = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)', 'Y = r"(?:CUST|PART|ORG)-[A-Z2-7]{4}"',
                 'Z = r"F-[A-Z2-7]{4}"', 'W = r"M-[A-Z]{24}"'):
        assert not LOCAL.search(line), line
