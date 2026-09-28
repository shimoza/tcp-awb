"""Package 4, the review pipeline, second part (document 06): knowledge entries per claim (V-04), the tier he
lowers (decision 14), the fuller record with counts per lens (V-12, V-18), the outcome softened, the send gate
(decision 15), the order and the finding shape of the review workflow (V-09, V-11, V-12, V-14), the request check
(V-16) and the calibration set (section 8).

Every text is invented; the only names are those of tests/fixtures.py.
"""
from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path

import pytest

from awb import bucket, calibrate, cli, kb, projects, review
from tests import fixtures as fx
from tests.obs_fake import FakeOBS
from tests.test_review import (HARNESS, NAME, NODE, WORKFLOW, needs_node, project, run_workflow,  # noqa: F401
                               scenario, write_lens_files)

GOAL = "size one cluster for the test"
TEXT = "The cluster of CUST-Q7M4 runs 3 nodes of s3.large.2 in eu-de.\n"
REQUEST = "How many nodes fit the quota of the test?\n"
TODAY = datetime.date.today()
REQUEST_WORKFLOW = WORKFLOW.parent / "request-check.js"


def spawn(home, register_path, customer=fx.CUSTOMER_CODE, kind="engagement") -> Path:
    pr = projects.spawn(home, kind, GOAL, customer, register_path)
    return Path(pr.path)


def reviewed(folder: Path, tmp_path: Path, tier: int, *, lowered=None, lenses=(), evidence=None,
             name: str = "reply.md", text: str = TEXT) -> Path:
    """A deliverable with a contract, a filled claim list and the given lens files, passed."""
    d = folder / "deliverables" / name
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text(text, encoding="utf-8")
    req = tmp_path / "request.txt"
    req.write_text(REQUEST, encoding="utf-8")
    review.init(d, req, tier, 400, lowered=lowered, force=True)
    review.write_claims(d)
    fill_all(folder, name, evidence)
    ldir = review.review_dir(folder, name) / review.LENSES
    for stem, data in lenses:
        ldir.mkdir(parents=True, exist_ok=True)
        (ldir / (stem + ".json")).write_text(json.dumps(data), encoding="utf-8")
    res = review.run_pass(d)
    assert res.passed, res.problems
    return d


def fill_all(folder: Path, name: str, evidence: str | None = None) -> None:
    if evidence is None:
        evidence = "evidence/source.md"
        (folder / evidence).parent.mkdir(parents=True, exist_ok=True)
        (folder / evidence).write_text("an invented source\n", encoding="utf-8")
    path = review.review_dir(folder, name) / review.CLAIMS
    rows = [review.Claim(r.id, r.line, r.kind, r.risk, r.sentence, evidence, "docs", "supported")
            for r in review.load_claims(path)]
    path.write_text(review.render_claims(rows), encoding="utf-8")


PLATFORM_LENS = {"kind": "platform", "findings": [{"id": "platform-tcp-1", "blocking": True,
                                                    "outcome": "confirmed-fixed"}]}
REQUEST_LENS = {"kind": "request", "findings": []}


# --------------------------------------------------------------------------- V-04 knowledge per claim


def add_entry(home, statement: str, grade: str, checked: str, cls: str = "stable") -> str:
    e = kb.add(statement, scope="tcp", tags=["ecs"], grade=grade, cls=cls, source="an invented source",
               checked=checked, force_new=True, where=home, register_path=home.register, today=TODAY)
    return e.id


def test_the_claims_get_their_knowledge_entries_and_one_can_settle_a_claim(home, register_path, tmp_path):
    live = add_entry(home, "The flavor s3.large.2 runs 3 nodes per cluster in eu-de in the invented test.", "live",
                     TODAY.isoformat())
    docs = add_entry(home, "The docs list s3.large.2 as a general purpose flavor of eu-de.", "docs",
                     TODAY.isoformat())
    old = add_entry(home, "In eu-de the flavor s3.large.2 was sold out for a day in the invented test.", "live",
                    (TODAY - datetime.timedelta(days=60)).isoformat(), cls="availability")
    folder = spawn(home, register_path)
    d = folder / "deliverables" / "reply.md"
    d.write_text(TEXT, encoding="utf-8")
    req = tmp_path / "request.txt"
    req.write_text(REQUEST, encoding="utf-8")
    review.init(d, req, 2, 400)
    res = review.write_claims(d)
    assert res.kb_rows == 1 and res.kb_path.name == "kb.tsv"
    table = res.kb_path.read_text(encoding="utf-8").splitlines()
    assert table[0] == "id\trisk\tentries"
    cells = table[1].split("\t")[2]
    assert "%s live %s settles" % (live, TODAY.isoformat()) in cells
    assert "%s docs" % docs in cells and "lead" in cells and "%s live" % old in cells and "expired" in cells

    def l0_with(evidence: str) -> list[str]:
        fill_all(folder, "reply.md", evidence)
        return [p for p in review.l0(d).problems if "claim" in p]

    assert l0_with("kb:%s" % live) == []
    assert any("only with the grade live or contract" in p for p in l0_with("kb:%s" % docs))
    assert any("has expired" in p for p in l0_with("kb:%s" % old))
    assert any("does not exist" in p for p in l0_with("kb:KB-ZZZZ"))
    kb.retire(live, "replaced in the test", where=home, register_path=home.register, today=TODAY)
    assert any("is retired" in p for p in l0_with("kb:%s" % live))


# --------------------------------------------------------------------------- decision 14, the lowered tier


def test_he_lowers_the_tier_in_the_contract_and_his_words_are_checked(home, register_path, tmp_path):
    folder = spawn(home, register_path)
    d = folder / "deliverables" / "reply.md"
    d.write_text(TEXT, encoding="utf-8")
    req = tmp_path / "request.txt"
    req.write_text(REQUEST, encoding="utf-8")
    target = review.init(d, req, 1, 400, lowered="a short answer, tier 1 is enough")
    assert "- lowered: a short answer, tier 1 is enough" in target.read_text(encoding="utf-8")
    assert review.load_contract(target).lowered == "a short answer, tier 1 is enough"
    with pytest.raises(review.ReviewError, match="below 3"):
        review.init(d, req, 3, 400, lowered="no need", force=True)
    with pytest.raises(review.Refused):
        review.init(d, req, 1, 400, lowered="ask %s first" % fx.CUSTOMER_FORMS[0], force=True)
    review.init(d, req, 2, 400, force=True)
    assert "lowered" not in (review.review_dir(folder, "reply.md") / review.CONTRACT).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- V-12, V-18 the record


def test_the_record_keeps_counts_per_lens_claims_contract_run_and_open_points(home, register_path, tmp_path):
    folder = spawn(home, register_path)
    lenses = [
        ("verify", {"kind": "verify", "dropped": 0, "run": {"agents": 7, "tokens": 51000}, "findings": [
            {"id": "verify-1", "severity": "blocking", "outcome": "refuted"}]}),
        ("platform-tcp", {"kind": "platform", "dropped": 2, "findings": [
            {"id": "platform-tcp-1", "severity": "blocking", "outcome": "softened"},
            {"id": "platform-tcp-2", "severity": "blocking", "outcome": "confirmed-fixed"},
            {"id": "platform-tcp-3", "severity": "major", "outcome": "open"}]}),
        ("request-check", {"kind": "request", "findings": [
            {"id": "request-1", "severity": "major", "outcome": "plausible"}]}),
    ]
    d = reviewed(folder, tmp_path, 3, lenses=lenses)
    record = json.loads((review.review_dir(folder, "reply.md") / review.RECORD).read_text(encoding="utf-8"))
    per = {r["lens"]: r for r in record["per_lens"]}
    assert set(per) == {"lens 1 (platform-tcp)", "lens 2 (request-check)", "lens 3 (verify)"}
    tcp = per["lens 1 (platform-tcp)"]
    assert (tcp["kind"], tcp["findings"], tcp["blocking"], tcp["open_blocking"], tcp["softened"],
            tcp["confirmed_fixed"], tcp["open_other"], tcp["dropped"]) == ("platform", 3, 2, 0, 1, 1, 1, 2)
    assert per["lens 2 (request-check)"]["plausible"] == 1 and per["lens 3 (verify)"]["refuted"] == 1
    assert record["run"] == {"agents": 7, "tokens": 51000}
    assert record["claims"]["rows"] >= 1 and record["claims"]["by_verdict"] == {"supported": record["claims"]["rows"]}
    assert record["contract"] == {"tier": 3, "budget": 400, "mode": "doc", "scope": "tcp", "lowered": False,
                                  "questions": 1, "ticked": 0}
    assert record["open_points"] == {"unknown_claims": 0, "open_findings": 2}
    assert review.status(folder)[0]["state"] == review.VALID
    assert d.exists()


def test_softened_closes_a_blocking_finding_and_plausible_does_not(home, register_path, tmp_path):
    folder = spawn(home, register_path)
    lens = review.review_dir(folder, "x.md") / review.LENSES
    lens.mkdir(parents=True)
    (lens / "platform-tcp.json").write_text(json.dumps({"findings": [
        {"severity": "blocking", "outcome": "softened"}, {"severity": "blocking", "outcome": "plausible"}]}),
        encoding="utf-8")
    summary, problems = review.lens_summary(lens, 3)
    assert summary["open_blocking"] == 1 and len(problems) == 1


# --------------------------------------------------------------------------- decision 15, the send gate


@pytest.fixture
def fake():
    with FakeOBS() as f:
        yield f


class Put:
    """bucket.put against the fake object store, returning (key or None, the error text, the warning)."""

    def __init__(self, home, code, capsys, fake):
        self.home, self.code, self.capsys, self.fake = home, code, capsys, fake

    def __call__(self, f: Path):
        self.capsys.readouterr()
        try:
            key, _ = bucket.put(self.home, self.fake.client(), self.code, f, replace=True)
            err = ""
        except bucket.BucketError as exc:
            key, err = None, str(exc)
        return key, err, self.capsys.readouterr().err


def code_of(folder: Path) -> str:
    return folder.name


def test_the_send_gate_refuses_what_a_customer_should_not_get(home, register_path, tmp_path, capsys, fake):
    folder = spawn(home, register_path)
    put = Put(home, code_of(folder), capsys, fake)
    draft = folder / "deliverables" / "draft.md"
    draft.write_text(TEXT, encoding="utf-8")
    key, err, _ = put(draft)
    assert key is None and "no valid review record (missing)" in err
    low = reviewed(folder, tmp_path, 1, name="low.md")
    key, err, _ = put(low)
    assert key is None and "needs tier 3 unless he lowered it" in err
    lowered = reviewed(folder, tmp_path, 1, name="lowered.md", lowered="a one-line answer")
    key, err, _ = put(lowered)
    assert key and not err
    full = reviewed(folder, tmp_path, 3, name="full.md", lenses=[("platform-tcp", PLATFORM_LENS)])
    key, err, _ = put(full)
    assert key is None and "request check has not run" in err
    full = reviewed(folder, tmp_path, 3, name="full.md", lenses=[("platform-tcp", PLATFORM_LENS),
                                                                ("request-check", REQUEST_LENS)])
    key, err, warn = put(full)
    assert key and not err and not warn
    copy = tmp_path / "copy-of-full.md"
    copy.write_bytes(full.read_bytes())
    assert put(copy)[0] and not put(copy)[2]                     # a copy of the reviewed text is that text
    full.write_text(TEXT + "One more line.\n", encoding="utf-8")
    key, err, _ = put(full)
    assert key is None and "(stale)" in err
    note = tmp_path / "notes.md"
    note.write_text("internal notes of the day\n", encoding="utf-8")
    key, err, warn = put(note)
    assert key and "internal file" in warn
    for text in (err, warn):
        assert "draft.md" not in text and "notes.md" not in text


def test_the_send_gate_only_warns_in_a_project_without_a_customer(home, register_path, tmp_path, capsys, fake):
    folder = spawn(home, register_path, customer=None, kind="lab")
    put = Put(home, code_of(folder), capsys, fake)
    draft = folder / "deliverables" / "draft.md"
    draft.write_text(TEXT, encoding="utf-8")
    key, err, warn = put(draft)
    assert key and not err and "no valid review record" in warn
    verdict, message = review.send_check(folder, reviewed(folder, tmp_path, 1, name="ok.md"), False)
    assert (verdict, message) == (review.SEND_OK, "reviewed at tier 1")


# --------------------------------------------------------------------------- the review workflow


@needs_node
def test_the_lenses_read_the_blind_results_and_the_prepare_step_takes_unsettled_claims(project, tmp_path):
    out = run_workflow(tmp_path, scenario(project))
    agents = [c for c in out["calls"] if c["kind"] == "agent"]
    last_blind = max(i for i, c in enumerate(agents) if c["phase"] == "Blind verification")
    first_lens = min(i for i, c in enumerate(agents) if c["phase"] == "Lenses")
    assert last_blind < first_lens
    for c in agents:
        if c["phase"] == "Lenses":
            assert "The blind verification ran before you" in c["prompt"]
            assert "- C-1 (line 3): contradicted" in c["prompt"]
    assert "an empty verdict column" in agents[0]["prompt"]
    verify = json.loads([f["content"] for f in out["files"] if f["path"].endswith("/verify.json")][0])
    assert verify["run"]["agents"] >= 5 and verify["run"]["tokens"] == 0


@needs_node
def test_a_finding_without_a_quote_or_evidence_is_dropped_and_counted(project, tmp_path):
    out = run_workflow(tmp_path, scenario(project))
    folder = write_lens_files(project, out["files"])
    tcp = json.loads((folder / "platform-tcp.json").read_text(encoding="utf-8"))
    assert tcp["dropped"] == 1 and [f["class"] for f in tcp["findings"]] == ["WRONG"]
    counts = {r["lens"]: r for r in review.lens_counts(folder)}
    assert counts["lens 3 (platform-tcp)"]["dropped"] == 1
    shaped = scenario(project, platform={"findings": [
        {"line": 7, "class": "EXTRA", "severity": "minor", "summary": "a link nobody asked for", "quote": "The guide is",
         "evidence": "the contract asks for flavors", "replacement": ""}]})
    out = run_workflow(tmp_path, shaped)
    tcp = json.loads([f["content"] for f in out["files"] if f["path"].endswith("/platform-tcp.json")][0])
    assert tcp["dropped"] == 0 and tcp["findings"][0]["quote"] == "The guide is"


@needs_node
def test_a_refute_agent_that_cannot_settle_leaves_the_outcome_plausible(project, tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS.replace("return { refuted: scenario.refute, reason: 'checked', source: 'a source' }",
                                       "return { refuted: false, outcome: 'plausible', reason: 'not settled' }"),
                       encoding="utf-8")
    scen = tmp_path / "scenario.json"
    scen.write_text(json.dumps(scenario(project)), encoding="utf-8")
    res = subprocess.run([NODE, str(harness), str(WORKFLOW), str(scen)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    folder = write_lens_files(project, out["files"])
    outcomes = [f["outcome"] for lens in folder.glob("*.json")
                for f in json.loads(lens.read_text(encoding="utf-8"))["findings"] if f["blocking"]]
    assert outcomes and set(outcomes) == {"plausible"}
    summary, problems = review.lens_summary(folder, 3)
    assert summary["open_blocking"] == len(outcomes) and problems


# --------------------------------------------------------------------------- V-16 the request check

REQUEST_HARNESS = r"""
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
  calls.push({ kind: 'agent', phase: ph, label: opts.label || '', prompt: prompt })
  if (ph === 'Request check') return scenario.check
  if (ph === 'Record') {
    const path = prompt.match(/^path: (.*)$/m)[1]
    const content = prompt.split('----- begin -----\n')[1].split('----- end -----')[0]
    files.push({ path: path, content: content })
    return { path: path, same: true }
  }
  throw new Error('unexpected agent call in phase ' + ph)
}
async function parallel(thunks) { return Promise.all(thunks.map((t) => t())) }
async function pipeline(items, ...stages) { return Promise.all(items.map(async (item) => { let v = item; for (const s of stages) v = await s(v, item); return v })) }
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', 'budget', src)
body(scenario.args, agent, parallel, pipeline, phase, log, { total: null, spent: () => 0, remaining: () => Infinity })
  .then((result) => process.stdout.write(JSON.stringify({ result: result, calls: calls, files: files })))
  .catch((err) => { process.stderr.write(String((err && err.stack) || err)); process.exit(3) })
"""


def run_request_check(tmp_path: Path, project: Path, check) -> dict:
    harness = tmp_path / "request-harness.js"
    harness.write_text(REQUEST_HARNESS, encoding="utf-8")
    scen = tmp_path / "request-scenario.json"
    scen.write_text(json.dumps({"args": {"project": str(project), "deliverable": "deliverables/" + NAME,
                                         "date": "2026-09-25"}, "check": check}), encoding="utf-8")
    res = subprocess.run([NODE, str(harness), str(REQUEST_WORKFLOW), str(scen)], capture_output=True, text=True,
                         timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@needs_node
def test_the_request_check_is_a_lens_of_its_own(project, tmp_path):
    src = REQUEST_WORKFLOW.read_text(encoding="utf-8")
    assert src.startswith("export const meta = {") and "phase('Request check')" in src and "phase('Record')" in src
    wrapper = tmp_path / "rc.js"
    wrapper.write_text("(async () => {\n" + src.replace("export const meta", "const meta", 1) + "\n})()\n",
                       encoding="utf-8")
    assert subprocess.run([NODE, "--check", str(wrapper)], capture_output=True, timeout=60).returncode == 0
    out = run_request_check(tmp_path, project, {
        "questions": [{"question": "Which backup options fit the landing zone?", "answered": True, "line": 3},
                      {"question": "Which flavors are named?", "answered": False}],
        "extra": [{"line": 7, "summary": "a link nobody asked for"}],
        "unlisted": [{"line": 6, "summary": "a statement of fact without a row"}],
        "overclaims": [{"claim": "C-1", "line": 3, "grade": "said", "summary": "a negative stated as a fact"}]})
    prompts = [c["prompt"] for c in out["calls"] if c["kind"] == "agent" and c["phase"] == "Request check"]
    assert len(prompts) == 1 and "contract.md" in prompts[0] and "claims.tsv" in prompts[0]
    assert "lenses/" not in prompts[0]                                  # the final gate sees no lens result
    assert [Path(f["path"]).name for f in out["files"]] == ["request-check.json"]
    folder = write_lens_files(project, out["files"])
    lens = json.loads((folder / "request-check.json").read_text(encoding="utf-8"))
    assert lens["kind"] == "request" and [q["answered"] for q in lens["questions"]] == [True, False]
    assert sorted((f["class"], f["blocking"]) for f in lens["findings"]) == [
        ("EXTRA", False), ("MISLEADING", True), ("MISREAD", True), ("UNSOURCED", False)]
    assert "Judge only sentences that have a row here" in prompts[0]
    summary, problems = review.lens_summary(folder, 3)
    assert summary["open_blocking"] == 2 and problems
    assert {r["kind"] for r in review.lens_counts(folder)} == {"request"}
    empty = run_request_check(tmp_path, project, None)
    lens = json.loads(empty["files"][0]["content"])
    assert [f["class"] for f in lens["findings"]] == ["OTHER"] and lens["findings"][0]["blocking"]


# --------------------------------------------------------------------------- the calibration set


def test_the_calibration_set_is_caught_by_the_script_steps_and_carries_no_name():
    data = calibrate.load()
    assert [n["form"] for n in data["names"]] == ["{customer}", "{person}", "{domain}"]
    text = calibrate.CASES_FILE.read_text(encoding="utf-8")
    fx.assert_no_fixture_name(text, "the calibration set")
    one, two = calibrate.invented_names(), calibrate.invented_names()
    assert one != two and one["mail"].endswith("@" + one["domain"]) and one["customer"].endswith(" GmbH")
    report = calibrate.run()
    steps = {r.step for r in report.rows}
    assert {"writing", "names", "l0", "keep", "workflow", "price", "clean"} <= steps
    assert report.missed == 0 and report.false_alarms == 0 and report.ok
    assert 20 <= len([r for r in report.rows if r.step != "clean"]) <= 30 and report.clean == 5
    assert report.caught == len([r for r in report.rows if r.step in calibrate.SCRIPT_STEPS])


def test_the_calibration_runner_can_fail(tmp_path, capsys, monkeypatch):
    data = calibrate.load()
    by_id = {c["id"]: c for c in data["cases"]}
    by_id["W1"]["text"] = "The quota in eu-de is fine and the flavor is the problem.\n"      # the defect removed
    by_id["K2"]["after"] = by_id["K2"]["before"]
    by_id["C1"]["text"] = "The team can leverage the quota in eu-de for the test.\n"          # a defect planted
    bad = tmp_path / "cases.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    report = calibrate.run(bad)
    states = {r.id: r.state for r in report.rows}
    assert (states["W1"], states["K2"], states["C1"]) == ("missed", "missed", "false alarm")
    assert report.missed == 2 and not report.ok
    monkeypatch.setattr(calibrate, "CASES_FILE", bad)
    assert cli.main(["review", "calibrate"]) == 1
    out = capsys.readouterr().out
    assert "caught 17 of 19 seeded defects" in out and "leverage" not in out
    assert cli.main(["review", "calibrate", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["missed"] == 2


def test_the_workflow_calibration_cases_are_invented_and_complete():
    data = json.loads((calibrate.REPO / "calibration" / "workflow-cases.json").read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) == 13 and sum(1 for c in cases if c["cls"] == "clean") == 5
    assert all(c["request"].endswith("?") and c["text"].startswith("# ") and c["expect"] for c in cases)
    text = json.dumps(data, ensure_ascii=False)
    fx.assert_no_fixture_name(text, "the workflow calibration cases")
    assert [c["id"] for c in cases if c.get("claims")] == ["m8"]          # the overclaim needs a graded row


def test_the_workflow_score_reads_the_lens_files(tmp_path):
    cases = {"cases": [
        {"id": "m1", "cls": "false-negative", "request": "Q?", "text": "# T\n", "expect": "x"},
        {"id": "m4", "cls": "content-nobody-asked-for", "request": "Q?", "text": "# T\n", "expect": "x"},
        {"id": "m9", "cls": "invented-flavor", "request": "Q?", "text": "# T\n", "expect": "x"},
        {"id": "c1", "cls": "clean", "request": "Q?", "text": "# T\n", "expect": "x"},
        {"id": "c2", "cls": "clean", "request": "Q?", "text": "# T\n", "expect": "x"},
        {"id": "c3", "cls": "clean", "request": "Q?", "text": "# T\n", "expect": "x"}]}
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")

    def lens(case, name, findings):
        d = tmp_path / "reviews" / ("%s.md" % case) / "lenses"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%s.json" % name)).write_text(json.dumps({"findings": findings}), encoding="utf-8")

    for name in calibrate.LENS_FILES:
        for case in ("m1", "m4", "m9", "c1", "c2"):
            lens(case, name, [])
    lens("m1", "verify", [{"class": "WRONG", "severity": "blocking", "outcome": "open"}])
    lens("m4", "request-check", [{"class": "EXTRA", "severity": "major", "outcome": "open"}])
    lens("m9", "partner", [{"class": "WRONG", "severity": "blocking", "outcome": "refuted"}])
    lens("c1", "partner", [{"class": "UNSOURCED", "blocking": True, "outcome": "open"}])
    lens("c3", "verify", [])
    rows = {r.id: r for r in calibrate.score_lenses(tmp_path, path)}
    assert {k: r.state for k, r in rows.items()} == {"m1": "caught", "m4": "caught", "m9": "missed",
                                                     "c1": "false alarm", "c2": "clean", "c3": "incomplete"}
    assert rows["m1"].by == ["verify: WRONG"] and rows["m4"].by == ["request-check: EXTRA"]
