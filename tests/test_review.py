"""Tests of awb/review.py (contract, claims, level-0 check, pass record, status) and workflows/review.js.

Every name here is an invented fixture name. Each test builds a small project folder under the temporary
projects root of the `home` fixture. The writing check (awb.writing) is another module: most tests put a small
stand-in into sys.modules, one test uses the real module when it is present. The workflow cannot run in a test:
it is checked with node on a wrapper and run once more with stub agents, and its lens files are read back with
review.lens_summary.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import types
from datetime import date
from pathlib import Path

import pytest

from awb import cli, review
from tests import fixtures

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / "workflows" / "review.js"
CODE = "tcp-q7m4"
NAME = "offer.md"

SENT_NEG = "Cross-region replication is not supported for this service."
SENT_PRICE = "The price is 0.12 EUR per hour."
SENT_FLAVOR = "We propose the flavor s9.large.2 for the web tier."
SENT_API = "The backup API is /v3/{project_id}/vaults and it lists every vault."
SENT_URL = "The guide is at https://docs.example.com/cbr/guide.html for details."
SENT_VERSION = "We use the provider in version 1.36.0 for this setup."

DELIVERABLE_TEXT = "\n".join([
    "# Offer for CUST-Q7M4",                  # line 1
    "",                                       # line 2
    SENT_NEG,                                 # line 3
    SENT_PRICE,                               # line 4
    SENT_FLAVOR,                              # line 5
    SENT_API,                                 # line 6
    SENT_URL,                                 # line 7
    SENT_VERSION,                             # line 8
    "",
    "It fits the landing zone of tcp-q7m4.",  # no claim
]) + "\n"

REQUEST_TEXT = "Which backup options fit the landing zone? Keep it short and name the flavors.\n"


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


# --------------------------------------------------------------------------- fixtures and helpers


@pytest.fixture
def project(home) -> Path:
    root = home.projects_root / CODE
    for sub in ("deliverables", "reviews", "evidence", "input"):
        (root / sub).mkdir(parents=True)
    (root / "deliverables" / ".gitkeep").write_bytes(b"")
    (root / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- platform: tcp\n- mode: sealed\n" % (CODE, CODE),
                                   encoding="utf-8")
    return root


@pytest.fixture
def writing(monkeypatch):
    """A stand-in for awb.writing: no tells unless a test sets some. Records every call."""
    calls: list[dict] = []
    state: dict = {"tells": []}

    def check_file(path, mode="doc", scope="tcp"):
        calls.append({"path": Path(path), "mode": mode, "scope": scope})
        return list(state["tells"]), {"sentences": 1}

    mod = types.ModuleType("awb.writing")
    mod.check_file = check_file
    monkeypatch.setitem(sys.modules, "awb.writing", mod)
    return types.SimpleNamespace(calls=calls, state=state)


def deliver(project: Path, text: str = DELIVERABLE_TEXT, name: str = NAME) -> Path:
    path = project / "deliverables" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def request_file(tmp_path: Path, text: str = REQUEST_TEXT) -> Path:
    path = tmp_path / "request.txt"
    path.write_text(text, encoding="utf-8")
    return path


def claims_path(project: Path, name: str = NAME) -> Path:
    return project / "reviews" / name / "claims.tsv"


def rows_of(project: Path, name: str = NAME) -> list[dict]:
    lines = claims_path(project, name).read_text(encoding="utf-8").splitlines()
    head = lines[0].split("\t")
    return [dict(zip(head, (line.split("\t") + [""] * 8)[:8])) for line in lines[1:] if line.strip()]


def row_with(project: Path, phrase: str, name: str = NAME) -> dict:
    found = [r for r in rows_of(project, name) if phrase in r["sentence"]]
    assert len(found) == 1, "%d rows carry the phrase" % len(found)
    return found[0]


def fill(project: Path, verdict: str = "supported", evidence: str = "evidence/source.md", which=None,
         name: str = NAME, **columns) -> None:
    """Set evidence, grade and verdict of the rows chosen by `which(row)` (default: every high-risk row)."""
    if evidence == "evidence/source.md":
        (project / evidence).write_text("source notes for the claims\n", encoding="utf-8")
    path = claims_path(project, name)
    lines = path.read_text(encoding="utf-8").splitlines()
    head = lines[0].split("\t")
    out = [lines[0]]
    chosen = which or (lambda r: r["risk"] == "high")
    for line in lines[1:]:
        cells = (line.split("\t") + [""] * 8)[:8]
        row = dict(zip(head, cells))
        if chosen(row):
            row.update(evidence=evidence, grade="docs", verdict=verdict)
            row.update(columns)
        out.append("\t".join(row[h] for h in head))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def start(project: Path, tmp_path: Path, tier: int = 2, budget: int = 400, text: str = DELIVERABLE_TEXT,
          mode: str = "doc") -> Path:
    d = deliver(project, text)
    review.init(d, request_file(tmp_path), tier, budget, mode=mode)
    review.write_claims(d)
    return d


# --------------------------------------------------------------------------- init


def test_init_writes_the_contract_with_its_fields(project, tmp_path, capsys):
    d = deliver(project)
    code, out, err = run(["review", "init", str(d), "--request", str(request_file(tmp_path)), "--tier", "2",
                          "--budget", "400", "--reader", "the platform team of CUST-Q7M4"], capsys)
    assert code == 0, err
    contract = project / "reviews" / NAME / "contract.md"
    text = contract.read_text(encoding="utf-8")
    lines = text.splitlines()
    for must in ("- deliverable: deliverables/offer.md", "- tier: 2", "- budget: 400 words", "- mode: doc",
                 "- scope: tcp", "- reader: the platform team of CUST-Q7M4", "- created: %s" % date.today().isoformat(),
                 "## Request as given", "> " + REQUEST_TEXT.strip(), "## Questions to answer",
                 "- [ ] Which backup options fit the landing zone?", "## Out of scope"):
        assert must in lines, "the contract misses a line"
    assert lines.index("## Request as given") < lines.index("## Questions to answer") < lines.index("## Out of scope")
    c = review.load_contract(contract)
    assert (c.tier, c.budget, c.mode, c.scope) == (2, 400, "doc", "tcp")
    assert "deliverables/offer.md" in out


def test_init_takes_the_mode_and_the_scope_of_the_project(project, tmp_path):
    # the platform is the prefix of the project code (the folder name): an hcs project gets scope hcs ...
    hcs_project = project.parent / "hcs-q7m4"
    shutil.copytree(project, hcs_project)
    (hcs_project / "SCOPE.md").write_text("# Scope\n\n- platform: hcs\n", encoding="utf-8")
    d = deliver(hcs_project)
    target = review.init(d, request_file(tmp_path), 1, 120, mode="mail")
    c = review.load_contract(target)
    assert (c.tier, c.budget, c.mode, c.scope) == (1, 120, "mail", "hcs")
    assert "- reader: not given" in target.read_text(encoding="utf-8").splitlines()
    # ... and a line in SCOPE.md of a tcp project does not turn the vendor-name rule off (review of release 2)
    (project / "SCOPE.md").write_text("# Scope\n\n- platform: hcs\n", encoding="utf-8")
    target = review.init(deliver(project), request_file(tmp_path), 1, 120, mode="mail")
    assert review.load_contract(target).scope == "tcp"


def test_init_refuses_a_request_with_a_fixture_form(project, tmp_path, capsys):
    d = deliver(project)
    req = request_file(tmp_path, "Please size the backup for %s by next week.\n" % fixtures.CUSTOMER_FORMS[0])
    code, out, err = run(["review", "init", str(d), "--request", str(req), "--tier", "2", "--budget", "400"], capsys)
    assert code == 1
    assert "refused by the name check" in err
    assert not (project / "reviews" / NAME / "contract.md").exists()
    fixtures.assert_no_fixture_name(out + err, "the output of a refused init")


def test_init_refuses_a_reader_with_a_fixture_form(project, tmp_path, capsys):
    d = deliver(project)
    code, out, err = run(["review", "init", str(d), "--request", str(request_file(tmp_path)), "--tier", "1",
                          "--budget", "400", "--reader", "the team of %s" % fixtures.PERSON_FORMS[0]], capsys)
    assert code == 1
    assert not (project / "reviews" / NAME / "contract.md").exists()
    fixtures.assert_no_fixture_name(out + err, "the output of a refused init")


def test_init_refuses_without_a_register(project, tmp_path, home):
    home.register.unlink()
    with pytest.raises(review.ReviewError, match="no register"):
        review.init(deliver(project), request_file(tmp_path), 2, 400)
    assert not (project / "reviews" / NAME / "contract.md").exists()


def test_init_needs_the_deliverables_folder_and_keeps_a_contract(project, tmp_path, capsys):
    outside = project / "offer.md"
    outside.write_text(DELIVERABLE_TEXT, encoding="utf-8")
    code, _, err = run(["review", "init", str(outside), "--request", str(request_file(tmp_path)), "--tier", "2",
                        "--budget", "400"], capsys)
    assert code == 2 and "deliverables folder" in err
    d = deliver(project)
    target = review.init(d, request_file(tmp_path), 2, 400)
    target.write_text(target.read_text(encoding="utf-8") + "- [x] his own question\n", encoding="utf-8")
    with pytest.raises(review.ReviewError, match="exists already"):
        review.init(d, request_file(tmp_path), 3, 400)
    assert "his own question" in target.read_text(encoding="utf-8")
    review.init(d, request_file(tmp_path), 3, 400, force=True)
    assert review.load_contract(target).tier == 3


def test_init_refuses_bad_tier_and_budget(project, tmp_path, capsys):
    d = deliver(project)
    req = str(request_file(tmp_path))
    assert run(["review", "init", str(d), "--request", req, "--tier", "4", "--budget", "400"], capsys)[0] == 2
    assert run(["review", "init", str(d), "--request", req, "--tier", "2", "--budget", "0"], capsys)[0] == 2
    assert run(["review", "init", str(d), "--request", req, "--tier", "2", "--budget", "many"], capsys)[0] == 2
    assert not (project / "reviews" / NAME / "contract.md").exists()


# --------------------------------------------------------------------------- claims


def test_claims_finds_each_kind_and_marks_the_risk(project, tmp_path, capsys):
    d = deliver(project)
    code, out, err = run(["review", "claims", str(d)], capsys)
    assert code == 0, err
    lines = claims_path(project).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "id\tline\tkind\trisk\tsentence\tevidence\tgrade\tverdict"
    rows = rows_of(project)
    expected = [
        (SENT_NEG, 3, "negative", "high"),
        (SENT_PRICE, 4, "price", "high"),
        (SENT_FLAVOR, 5, "identifier", "high"),
        (SENT_API, 6, "api-path", "high"),
        (SENT_URL, 7, "url", "low"),
        (SENT_VERSION, 8, "version", "medium"),
    ]
    assert len(rows) == len(expected)
    for (sentence, line, kind, risk), row in zip(expected, rows):
        assert row["sentence"] == sentence
        assert int(row["line"]) == line
        assert kind in row["kind"].split(",")
        assert row["risk"] == risk
        assert row["evidence"] == row["grade"] == row["verdict"] == ""
    assert [r["id"] for r in rows] == ["C-1", "C-2", "C-3", "C-4", "C-5", "C-6"]
    assert "6 claims" in out


def test_claims_numbers_codes_dates_and_layout():
    text = "\n".join([
        "## 2. Sizing",
        "",
        "We size 20 VMs with 8 vCPU each,",
        "160 vCPU in total.",
        "The team met 3 times.",
        "The kickoff for CUST-Q7M4 in tcp-q7m4 was on 2026-09-22.",
        "",
        "| item | price |",
        "|---|---|",
        "| backup | 4 EUR |",
        "",
        "```",
        "GET /v3/{project_id}/backups",
        "```",
        "- 12 users need access",
        "> the API /v2.0/networks is used.",
        "The subnet 192.0.2.0/24 was planned on 22.09.2026 at 10:30.",
    ])
    rows = review.find_claims(text)
    by = {r.sentence: r for r in rows}
    total = by["We size 20 VMs with 8 vCPU each, 160 vCPU in total."]
    assert (total.line, total.kind, total.risk) == (3, "number", "high")
    assert by["The team met 3 times."].risk == "medium"
    assert not [r for r in rows if "kickoff" in r.sentence or r.sentence == "Sizing" or "subnet" in r.sentence]
    assert by["| backup | 4 EUR |"].kind == "price" and by["| backup | 4 EUR |"].line == 10
    assert by["GET /v3/{project_id}/backups"].kind == "api-path"
    assert (by["12 users need access"].line, by["12 users need access"].risk) == (15, "high")
    assert (by["the API /v2.0/networks is used."].line, by["the API /v2.0/networks is used."].kind) == (16, "api-path")
    assert len(rows) == 6
    assert all(len(r.sentence) <= review.MAX_SENTENCE for r in review.find_claims("It is not " + "long " * 80 + "."))


def test_claims_keeps_evidence_and_verdict_when_written_again(project, tmp_path):
    d = deliver(project)
    review.write_claims(d)
    fill(project, verdict="unknown", evidence="evidence/source.md", which=lambda r: r["sentence"] == SENT_NEG)
    fill(project, verdict="supported", evidence="evidence/source.md", which=lambda r: r["sentence"] == SENT_PRICE)
    before = row_with(project, "replication")
    # a new sentence above shifts the lines, the price sentence goes away
    text = DELIVERABLE_TEXT.replace("\n\n" + SENT_NEG, "\n\nThe network has 2 zones.\n" + SENT_NEG)
    text = text.replace(SENT_PRICE + "\n", "")
    deliver(project, text)
    res = review.write_claims(d)
    after = row_with(project, "replication")
    assert after["id"] == before["id"] == "C-1"
    assert (after["evidence"], after["grade"], after["verdict"]) == ("evidence/source.md", "docs", "unknown")
    assert int(after["line"]) == 4
    assert not [r for r in rows_of(project) if "0.12" in r["sentence"]]
    new = row_with(project, "2 zones")
    assert new["id"] == "C-7" and new["verdict"] == "" and new["evidence"] == ""
    assert (res.kept, res.dropped) == (5, 1)


def test_claims_refuses_a_deliverable_with_a_name(project, capsys):
    d = deliver(project, DELIVERABLE_TEXT + "It was agreed with %s.\n" % fixtures.CUSTOMER_FORMS[1])
    code, out, err = run(["review", "claims", str(d)], capsys)
    assert code == 1
    assert not claims_path(project).exists()
    fixtures.assert_no_fixture_name(out + err, "the output of a refused claims run")


# --------------------------------------------------------------------------- l0


def test_l0_passes_with_evidence_and_verdicts(project, tmp_path, writing, capsys):
    d = start(project, tmp_path)
    fill(project)
    code, out, err = run(["review", "l0", str(d)], capsys)
    assert code == 0, out + err
    assert out.startswith("l0 passed: deliverables/offer.md (tier 2)")
    assert writing.calls and writing.calls[0]["mode"] == "doc" and writing.calls[0]["scope"] == "tcp"
    assert writing.calls[0]["path"] == d


def test_l0_blocks_a_high_risk_claim_without_evidence(project, tmp_path, writing, capsys):
    d = start(project, tmp_path)
    fill(project, which=lambda r: r["risk"] == "high" and r["sentence"] != SENT_API)
    api = row_with(project, "/v3/")
    code, out, _ = run(["review", "l0", str(d)], capsys)
    assert code == 1
    assert "- claim %s (line 6, high): no evidence" % api["id"] in out.splitlines()
    assert "- claim %s (line 6, high): no verdict" % api["id"] in out.splitlines()
    assert review.l0(d).blocked


def test_l0_blocks_evidence_that_is_not_a_project_path(project, tmp_path, writing):
    d = start(project, tmp_path)
    (tmp_path / "outside.md").write_text("notes\n", encoding="utf-8")
    cases = {
        "evidence/missing.md": "does not exist",
        "../outside.md": "leaves the project",
        "deliverables/offer.md": "its own evidence",
        "https://docs.example.com/cbr": "not a link",
    }
    for evidence, reason in cases.items():
        fill(project, evidence=evidence)
        res = review.l0(d)
        assert res.blocked and any(reason in p for p in res.problems), reason
    (project / "evidence" / "source.md").write_text("notes\n", encoding="utf-8")
    fill(project, evidence="evidence/source.md; input")
    assert not review.l0(d).blocked


def test_l0_blocks_contradicted(project, tmp_path, writing):
    d = start(project, tmp_path, tier=1)
    fill(project)
    fill(project, verdict="contradicted", which=lambda r: r["sentence"] == SENT_FLAVOR)
    res = review.l0(d)
    assert res.blocked
    assert [p for p in res.problems if p.endswith(": contradicted")] == ["claim C-3 (line 5, high): contradicted"]


def test_l0_blocks_unknown_at_tier_2_but_not_at_tier_1(project, tmp_path, writing):
    d = start(project, tmp_path, tier=1)
    fill(project, verdict="unknown")
    assert not review.l0(d).blocked
    review.init(d, request_file(tmp_path), 2, 400, force=True)
    res = review.l0(d)
    assert res.blocked and res.tier == 2
    assert sum("unknown, which blocks from tier 2" in p for p in res.problems) == 4


def test_l0_blocks_when_over_budget(project, tmp_path, writing, capsys):
    d = start(project, tmp_path, budget=20)
    fill(project)
    words = review.count_words(DELIVERABLE_TEXT)
    assert words > 20
    code, out, _ = run(["review", "l0", str(d)], capsys)
    assert code == 1
    assert "- words: %d, over the budget of 20" % words in out.splitlines()
    review.init(d, request_file(tmp_path), 2, words, force=True)
    assert not review.l0(d).blocked


def test_l0_blocks_a_blocking_tell_and_uses_the_mode_of_the_contract(project, tmp_path, writing, capsys):
    d = start(project, tmp_path, mode="mail")
    fill(project)
    writing.state["tells"] = [
        {"line": 3, "cls": "em-dash", "blocking": True, "hint": "no em-dash"},
        types.SimpleNamespace(line=4, cls="we-in-mail", blocking=True, hint="first person"),
        {"line": 5, "cls": "long-words", "blocking": False, "hint": "shorter words"},
    ]
    code, out, _ = run(["review", "l0", str(d)], capsys)
    assert code == 1
    assert "- writing: em-dash line 3" in out.splitlines()
    assert "- writing: we-in-mail line 4" in out.splitlines()
    assert "long-words" not in out
    assert writing.calls[-1]["mode"] == "mail"


def test_l0_blocks_when_the_writing_check_is_missing(project, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "awb.writing", None)
    d = start(project, tmp_path)
    fill(project)
    res = review.l0(d)
    assert res.blocked and "writing: the writing check is not installed" in res.problems


@pytest.mark.skipif(importlib.util.find_spec("awb.writing") is None, reason="awb.writing is not built yet")
def test_l0_uses_the_real_writing_check_when_present(project, tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "awb.writing", raising=False)
    d = start(project, tmp_path, text=DELIVERABLE_TEXT + "It runs in two zones " + chr(0x2014) + " both are active.\n")
    fill(project)
    res = review.l0(d)
    assert res.blocked
    assert any(p.startswith("writing: em-dash line") for p in res.problems)


def test_l0_blocks_a_name_and_prints_no_value(project, tmp_path, writing, capsys):
    d = start(project, tmp_path)
    fill(project)
    deliver(project, DELIVERABLE_TEXT.replace("It fits", "For %s it fits" % fixtures.CUSTOMER_FORMS[1]))
    code, out, err = run(["review", "l0", str(d)], capsys)
    assert code == 1
    assert "- name check: 1 hits (name 1)" in out.splitlines()
    fixtures.assert_no_fixture_name(out + err, "the output of l0")


def test_l0_blocks_a_claim_that_is_not_listed_and_ignores_the_risk_column(project, tmp_path, writing):
    d = start(project, tmp_path)
    fill(project)
    deliver(project, DELIVERABLE_TEXT + "Snapshots are not kept longer than 30 days.\n")
    res = review.l0(d)
    assert res.blocked and any("misses 1 claims" in p and "(lines 11)" in p for p in res.problems)
    review.write_claims(d)
    # the new row is talked down by hand: the risk still comes from the detector
    fill(project, verdict="", evidence="", which=lambda r: "Snapshots" in r["sentence"], risk="low")
    res = review.l0(d)
    assert res.blocked
    assert any(p.endswith("(line 11, high): no evidence") for p in res.problems)


def test_l0_checks_grade_and_verdict_words_and_needs_a_contract(project, tmp_path, writing, capsys):
    d = deliver(project)
    code, _, err = run(["review", "l0", str(d)], capsys)
    assert code == 2 and "contract.md" in err
    start(project, tmp_path)
    fill(project, verdict="fine", which=lambda r: r["sentence"] == SENT_URL)
    fill(project, which=lambda r: r["risk"] == "high")
    res = review.l0(d)
    assert res.blocked
    assert any("the verdict must be" in p for p in res.problems)


# --------------------------------------------------------------------------- pass and status


def test_pass_writes_the_record_with_the_sha256(project, tmp_path, writing, capsys):
    d = start(project, tmp_path)
    fill(project)
    code, out, err = run(["review", "pass", str(d)], capsys)
    assert code == 0, out + err
    record = json.loads((project / "reviews" / NAME / "record.json").read_text(encoding="utf-8"))
    assert record["sha256"] == hashlib.sha256(d.read_bytes()).hexdigest()
    assert record["tier"] == 2
    assert record["date"] == date.today().isoformat()
    assert record["deliverable"] == "deliverables/offer.md"
    assert record["l0"]["blocked"] is False and record["l0"]["claims"] == 6 and record["l0"]["high"] == 4
    assert record["lenses"]["files"] == 0 and record["lenses"]["required"] is False


def test_a_failed_pass_writes_no_record_and_removes_an_old_one(project, tmp_path, writing, capsys):
    d = start(project, tmp_path)
    fill(project)
    assert review.run_pass(d).passed
    record = project / "reviews" / NAME / "record.json"
    assert record.exists()
    fill(project, verdict="contradicted", which=lambda r: r["sentence"] == SENT_PRICE)
    code, out, _ = run(["review", "pass", str(d)], capsys)
    assert code == 1 and out.startswith("pass blocked")
    assert not record.exists()


def lens(project: Path, stem: str, findings: list[dict]) -> Path:
    folder = project / "reviews" / NAME / "lenses"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (stem + ".json")
    path.write_text(json.dumps({"lens": stem, "findings": findings}), encoding="utf-8")
    return path


def test_tier_3_pass_needs_lens_files_without_open_blocking_findings(project, tmp_path, writing):
    d = start(project, tmp_path, tier=3)
    fill(project)
    res = review.run_pass(d)
    assert not res.passed and any("needs the lens results" in p for p in res.problems)

    platform = lens(project, "platform-tcp", [
        {"id": "platform-tcp-1", "severity": "blocking", "blocking": True, "outcome": "open"},
        {"id": "platform-tcp-2", "severity": "minor", "outcome": "open"},
    ])
    partner = lens(project, "partner", [{"id": "partner-1", "severity": "blocking"}])
    res = review.run_pass(d)
    assert not res.passed
    assert res.lenses["open_blocking"] == 2 and res.lenses["files"] == 2
    assert any(p.startswith("lens 2 (platform-tcp): 1 blocking") for p in res.problems)

    platform.write_text(json.dumps({"findings": [
        {"id": "platform-tcp-1", "blocking": True, "outcome": "confirmed-fixed"},
        {"id": "platform-tcp-2", "severity": "minor", "outcome": "open"}]}), encoding="utf-8")
    partner.write_text(json.dumps({"findings": [{"id": "partner-1", "severity": "blocking", "outcome": "refuted"}]}),
                       encoding="utf-8")
    res = review.run_pass(d)
    assert res.passed, res.problems
    assert res.record["tier"] == 3
    assert res.record["lenses"] == {"required": True, "files": 2, "findings": 3, "blocking": 2, "open_blocking": 0,
                                    "refuted": 1, "confirmed_fixed": 1}

    partner.write_text("{not json", encoding="utf-8")
    res = review.run_pass(d)
    assert not res.passed and any("cannot be read as JSON" in p for p in res.problems)


def test_lens_files_present_at_tier_2_are_checked_too(project, tmp_path, writing):
    d = start(project, tmp_path, tier=2)
    fill(project)
    lens(project, "verify", [{"id": "verify-1", "severity": "blocking", "outcome": "open"}])
    assert not review.run_pass(d).passed
    lens(project, "verify", [{"id": "verify-1", "severity": "blocking", "outcome": "refuted"}])
    assert review.run_pass(d).passed


def test_status_valid_then_stale_then_missing(project, tmp_path, writing, capsys):
    d = start(project, tmp_path)
    fill(project)
    assert review.run_pass(d).passed
    assert review.status(project) == [{"file": "deliverables/offer.md", "state": "valid", "tier": 2}]
    code, out, _ = run(["review", "status", str(project)], capsys)
    assert code == 0 and out.split() == ["valid", "deliverables/offer.md"]

    d.write_text(DELIVERABLE_TEXT + "One more line.\n", encoding="utf-8")
    assert [r["state"] for r in review.status(project)] == ["stale"]

    deliver(project, "A second text.\n", name="mail.md")
    rows = review.status(project)
    assert [(r["file"], r["state"]) for r in rows] == [("deliverables/mail.md", "missing"),
                                                       ("deliverables/offer.md", "stale")]
    code, out, _ = run(["review", "status", str(project)], capsys)
    assert code == 1
    assert out.splitlines() == ["missing  deliverables/mail.md", "stale    deliverables/offer.md"]


def test_status_masks_a_file_name_with_a_name(project, capsys):
    deliver(project, "A text.\n", name="offer-%s.md" % fixtures.CUSTOMER_FORMS[1])
    assert [r["state"] for r in review.status(project)] == ["missing"]
    code, out, err = run(["review", "status", str(project)], capsys)
    assert code == 1 and out.split() == ["missing", "file", "1"]
    fixtures.assert_no_fixture_name(out + err, "the output of status")


# --------------------------------------------------------------------------- the workflow


NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

PHASES = ["Prepare", "Blind verification", "Lenses", "Refute", "Record"]


def workflow_body() -> str:
    """The script with the export keyword removed, as the body of an async function."""
    src = WORKFLOW.read_text(encoding="utf-8")
    assert src.startswith("export const meta = {")
    return src.replace("export const meta", "const meta", 1)


def meta_block() -> str:
    src = WORKFLOW.read_text(encoding="utf-8")
    end = src.index("\n}\n")
    return src[len("export const meta = "):end + 2]


@needs_node
def test_workflow_passes_node_check_on_a_wrapper(tmp_path):
    wrapper = tmp_path / "review-check.js"
    wrapper.write_text("(async () => {\n" + workflow_body() + "\n})()\n", encoding="utf-8")
    res = subprocess.run([NODE, "--check", str(wrapper)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    broken = tmp_path / "broken.js"
    broken.write_text("(async () => {\nconst x: string = 'typed'\n})()\n", encoding="utf-8")
    assert subprocess.run([NODE, "--check", str(broken)], capture_output=True, timeout=60).returncode != 0


@needs_node
def test_workflow_meta_is_a_pure_literal_with_the_phases(tmp_path):
    block = meta_block()
    for bad in ("`", "${", "...", "("):
        assert bad not in block, "the meta block is not a pure literal"
    probe = tmp_path / "meta.js"
    probe.write_text("const vm = require('vm')\nprocess.stdout.write(JSON.stringify(vm.runInNewContext('(' + "
                     + json.dumps(block) + " + ')', {})))\n", encoding="utf-8")
    res = subprocess.run([NODE, str(probe)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    assert meta["name"] and meta["description"]
    assert [p["title"] for p in meta["phases"]] == PHASES
    src = WORKFLOW.read_text(encoding="utf-8")
    for title in PHASES:
        assert "phase('%s')" % title in src or "phase: '%s'" % title in src, title
    assert "/reviews/' + A.name" in src and "/lenses/'" in src
    for call in ("agent(", "parallel(", "pipeline("):
        assert call in src


HARNESS = r"""
const fs = require('fs')
const [scriptPath, scenarioPath] = process.argv.slice(2)
const src = fs.readFileSync(scriptPath, 'utf8').replace('export const meta', 'const meta')
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'))
const calls = []
const files = []
let current = ''
function phase(title) { current = title; calls.push({ kind: 'phase', title: title }) }
function log(message) { calls.push({ kind: 'log', message: message }) }
async function agent(prompt, opts) {
  opts = opts || {}
  const ph = opts.phase || current
  const label = opts.label || ''
  calls.push({ kind: 'agent', phase: ph, label: label, prompt: prompt })
  if (ph === 'Prepare') return scenario.prepare
  if (ph === 'Blind verification' && label.startsWith('blind ')) {
    const id = label.slice(6)
    return { answer: 'blind answer ' + id, sources: ['source ' + id], tried: ['one', 'two'], grade: 'docs',
             evidence_file: 'evidence/review/offer.md/' + id + '.md' }
  }
  if (ph === 'Blind verification' && label.startsWith('compare ')) {
    return { verdict: scenario.verdicts[label.slice(8)] || 'supported', reason: 'compared' }
  }
  if (ph === 'Lenses') return label === 'partner' ? scenario.partner : scenario.platform
  if (ph === 'Refute') return { refuted: scenario.refute, reason: 'checked', source: 'a source' }
  if (ph === 'Record') {
    const path = prompt.match(/^path: (.*)$/m)[1]
    const content = prompt.split('----- begin -----\n')[1].split('----- end -----')[0]
    files.push({ path: path, content: content })
    return { path: path, same: true }
  }
  throw new Error('unexpected agent call in phase ' + ph)
}
async function parallel(thunks) { return Promise.all(thunks.map((t) => t())) }
async function pipeline(items, ...stages) {
  return Promise.all(items.map(async (item, i) => {
    let v = item
    for (const stage of stages) v = await stage(v, item, i)
    return v
  }))
}
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', 'budget', src)
body(scenario.args, agent, parallel, pipeline, phase, log, { total: null, spent: () => 0, remaining: () => Infinity })
  .then((result) => process.stdout.write(JSON.stringify({ result: result, calls: calls, files: files })))
  .catch((err) => { process.stderr.write(String((err && err.stack) || err)); process.exit(3) })
"""

GOOD_ANSWERS = {"P4": "no", "P6": "no", "P7": "no"}


def scenario(project: Path, *, tier: int = 3, refute: bool = False, platform="default", partner_bad=("P4",)) -> dict:
    answers = [{"id": "P%d" % n, "answer": GOOD_ANSWERS.get("P%d" % n, "yes"), "line": 0, "note": "checked"}
               for n in range(1, 12)]
    for a in answers:
        if a["id"] in partner_bad:
            a["answer"] = "yes" if a["answer"] == "no" else "no"
    if platform == "default":
        platform = {"findings": [
            {"line": 5, "claim": "C-3", "class": "WRONG", "severity": "blocking", "summary": "the flavor family is "
             "offered elsewhere", "source": "docs"},
            {"line": 7, "class": "EXTRA", "severity": "minor", "summary": "a link nobody asked for"},
        ]}
    return {
        "args": {"project": str(project), "deliverable": "deliverables/" + NAME, "platforms": ["tcp", "azure"],
                 "date": "2026-09-22"},
        "prepare": {"tier": tier, "questions": [
            {"claim": "C-1", "line": 3, "kind": "negative", "platform": "tcp", "sentence": SENT_NEG,
             "question": "Which regions offer replication across regions for this service?"},
            {"claim": "C-3", "line": 5, "kind": "identifier", "platform": "tcp", "sentence": SENT_FLAVOR,
             "question": "Which flavor families are offered for web servers?"},
        ]},
        "verdicts": {"C-1": "contradicted", "C-3": "supported"},
        "platform": platform,
        "partner": {"answers": answers},
        "refute": refute,
    }


def run_workflow(tmp_path: Path, data: dict) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    scen = tmp_path / "scenario.json"
    scen.write_text(json.dumps(data), encoding="utf-8")
    res = subprocess.run([NODE, str(harness), str(WORKFLOW), str(scen)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def write_lens_files(project: Path, files: list[dict]) -> Path:
    folder = project / "reviews" / NAME / "lenses"
    for f in files:
        path = Path(f["path"])
        assert path.parent == folder
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(f["content"], encoding="utf-8")
    return folder


@needs_node
def test_workflow_writes_lens_files_that_the_pass_reads(project, tmp_path, home):
    out = run_workflow(tmp_path, scenario(project))
    names = sorted(Path(f["path"]).name for f in out["files"])
    assert names == ["partner.json", "platform-azure.json", "platform-tcp.json", "verify.json"]
    agents = [c for c in out["calls"] if c["kind"] == "agent"]
    assert {c["phase"] for c in agents} == set(PHASES)
    # one refute agent for every blocking finding: C-1 contradicted, one per platform lens, the partner answer P4
    assert len([c for c in agents if c["phase"] == "Refute"]) == 4
    folder = write_lens_files(project, out["files"])
    summary, problems = review.lens_summary(folder, 3)
    assert summary["files"] == 4 and summary["blocking"] == 4 and summary["open_blocking"] == 4
    assert len(problems) == 4
    verify = json.loads((folder / "verify.json").read_text(encoding="utf-8"))
    assert verify["deliverable"] == "deliverables/" + NAME and verify["date"] == "2026-09-22"
    assert [(c["claim"], c["verdict"]) for c in verify["claims"]] == [("C-1", "contradicted"), ("C-3", "supported")]
    assert [f["claim"] for f in verify["findings"]] == ["C-1"]
    assert all("context" not in f for f in verify["findings"])
    assert sum(r["open_blocking"] for r in out["result"]["lenses"]) == 4

    out = run_workflow(tmp_path, scenario(project, refute=True))
    folder = write_lens_files(project, out["files"])
    summary, problems = review.lens_summary(folder, 3)
    assert summary["open_blocking"] == 0 and summary["refuted"] == 4 and problems == []


@needs_node
def test_workflow_blind_agents_never_see_the_claim(project, tmp_path):
    out = run_workflow(tmp_path, scenario(project))
    agents = [c for c in out["calls"] if c["kind"] == "agent"]
    blind = [c for c in agents if c["label"].startswith("blind ")]
    compare = [c for c in agents if c["label"].startswith("compare ")]
    assert len(blind) == len(compare) == 2
    for c in blind:
        assert SENT_NEG not in c["prompt"] and SENT_FLAVOR not in c["prompt"]
        assert "s9.large.2" not in c["prompt"] and "do not open the folders deliverables/ and reviews/" in c["prompt"]
    assert any(SENT_NEG in c["prompt"] for c in compare)
    partner = [c for c in agents if c["label"] == "partner"][0]
    assert all(re.search(r"^P%d " % n, partner["prompt"], re.M) for n in range(1, 12))


@needs_node
def test_workflow_a_lens_that_returns_nothing_stays_blocking(project, tmp_path):
    out = run_workflow(tmp_path, scenario(project, refute=True, platform=None, partner_bad=()))
    folder = write_lens_files(project, out["files"])
    summary, problems = review.lens_summary(folder, 3)
    assert summary["open_blocking"] == 2
    assert {p.split(":")[0] for p in problems} == {"lens 2 (platform-azure)", "lens 3 (platform-tcp)"}


@needs_node
def test_workflow_skips_below_tier_2(project, tmp_path):
    out = run_workflow(tmp_path, scenario(project, tier=1))
    assert out["result"]["skipped"] is True and out["files"] == []
    assert [c["phase"] for c in out["calls"] if c["kind"] == "agent"] == ["Prepare"]
