"""RV1, RV2 and T6 of 2026-10-27: the blind verification that checked nothing, the final check in place of the
request check and the blind verification; the scriptPath of the FX template line.

RV1: a session set the verdicts in claims.tsv before the tier 3 review, so the blind verification had 0 claims to
check and the run looked complete. `awb review pass` now refuses that and workflows/review.js stops after its
prepare step. RV2 (his decision): the final text of a recorded final check, unchanged since (the record carries
its sha256), gets a review of the platform lenses and the partner lens only (`after_fx`); the send gate takes
the final check for the request check. T6: the template names /opt/tcp-awb/src/workflows, the folder the guard
accepts through the release link. Invented codes only.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from awb import deploy, projects, review, rulesync, toolguard
from tests.test_fx_final_check import RESULT, run_final
from tests.test_review import NODE, WORKFLOW, needs_node, project, run_workflow, scenario, writing  # noqa: F401
from tests.test_review_board import (PLATFORM_LENS, REQUEST, REQUEST_LENS, TEXT, Put, fake, fill_all,  # noqa: F401
                                     reviewed, spawn)

DRAFT = "answer.md"
FINAL = "answer.final.md"
PARTNER_LENS = {"kind": "partner", "findings": []}
EMPTY_VERIFY = {"kind": "verify", "findings": [], "claims": []}


def start(folder: Path, tmp_path: Path, name: str, text: str = TEXT, tier: int = 3) -> Path:
    d = folder / "deliverables" / name
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text(text, encoding="utf-8")
    req = tmp_path / "request.txt"
    req.write_text(REQUEST, encoding="utf-8")
    review.init(d, req, tier, 400, force=True)
    review.write_claims(d)
    fill_all(folder, name)
    return d


def lenses(folder: Path, name: str, files) -> None:
    ldir = review.review_dir(folder, name) / review.LENSES
    ldir.mkdir(parents=True, exist_ok=True)
    for stem, data in files:
        (ldir / (stem + ".json")).write_text(json.dumps(data), encoding="utf-8")


def fx_record(folder: Path, text: Path | None, **over) -> Path:
    """The record final-check.js writes for the draft answer.md, with the hash of the final text when given."""
    data = {"kind": "final-check", "deliverable": "deliverables/" + DRAFT, "date": "2026-10-27",
            "model": "Fable 5.1", "effort": "max", "final": "deliverables/" + FINAL, "open_points": []}
    if text is not None:
        data["final_sha256"] = review.sha256_file(text)
    data.update(over)
    path = review.review_dir(folder, DRAFT) / review.FINAL_CHECK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- RV1, the pass


def test_rv1_a_blind_verification_of_0_claims_over_preset_verdicts_refuses_the_pass(home, register_path, tmp_path):
    folder = spawn(home, register_path)
    d = start(folder, tmp_path, "reply.md")
    lenses(folder, "reply.md", [("platform-tcp", PLATFORM_LENS), ("partner", PARTNER_LENS),
                                ("verify", EMPTY_VERIFY)])
    res = review.run_pass(d)
    assert res.l0.high > 0 and not res.passed
    hit = [p for p in res.problems if "blind verification checked 0 claims" in p]
    assert len(hit) == 1 and "clear the verdicts set before the review in claims.tsv" in hit[0]
    assert not (review.review_dir(folder, "reply.md") / review.RECORD).exists()
    claim = review.load_claims(review.review_dir(folder, "reply.md") / review.CLAIMS)[0].id
    lenses(folder, "reply.md", [("verify", {"kind": "verify", "findings": [],
                                            "claims": [{"claim": claim, "verdict": "supported"}]})])
    res = review.run_pass(d)
    assert res.passed, res.problems


def test_rv1_verdicts_of_an_earlier_blind_round_count_as_checked(home, register_path, tmp_path):
    folder = spawn(home, register_path)
    d = start(folder, tmp_path, "reply.md")
    path = review.review_dir(folder, "reply.md") / review.CLAIMS
    rows = review.load_claims(path)
    out = []
    for r in rows:
        blind = "%s/%s/%s.md" % (review.BLIND_EVIDENCE, review.blind_slug("reply.md"), r.id)
        (folder / blind).parent.mkdir(parents=True, exist_ok=True)
        (folder / blind).write_text("a blind answer of an earlier round\n", encoding="utf-8")
        out.append(review.Claim(r.id, r.line, r.kind, r.risk, r.sentence, blind, "docs", "supported"))
    path.write_text(review.render_claims(out), encoding="utf-8")
    lenses(folder, "reply.md", [("platform-tcp", PLATFORM_LENS), ("verify", EMPTY_VERIFY)])
    res = review.run_pass(d)
    assert res.passed, res.problems
    (folder / out[0].evidence).unlink()
    res = review.run_pass(d)
    assert not res.passed and any("checked 0 claims while 1 high-risk" in p for p in res.problems)


def test_rv1_leaves_tier_2_a_text_without_high_risk_claims_and_a_review_without_verify_as_before(
        home, register_path, tmp_path):
    folder = spawn(home, register_path)
    d = start(folder, tmp_path, "low.md", tier=2)
    lenses(folder, "low.md", [("verify", EMPTY_VERIFY)])
    assert review.run_pass(d).passed
    d = start(folder, tmp_path, "plain.md", text="The landing zone fits the plan of CUST-Q7M4.\n")
    lenses(folder, "plain.md", [("platform-tcp", PLATFORM_LENS), ("verify", EMPTY_VERIFY)])
    res = review.run_pass(d)
    assert res.l0.high == 0 and res.passed, res.problems
    d = start(folder, tmp_path, "nov.md")
    lenses(folder, "nov.md", [("platform-tcp", PLATFORM_LENS)])
    res = review.run_pass(d)
    assert res.passed and "final_check" not in res.record and res.final_check == ""


# --------------------------------------------------------------------------- RV1, the workflow


@needs_node
def test_rv1_the_workflow_stops_after_prepare_when_preset_verdicts_leave_nothing_blind(project, tmp_path):  # noqa: F811
    data = scenario(project)
    data["prepare"] = {"tier": 3, "preset": 2, "questions": []}
    harness = tmp_path / "harness.js"
    from tests.test_review import HARNESS
    harness.write_text(HARNESS, encoding="utf-8")
    scen = tmp_path / "scenario.json"
    scen.write_text(json.dumps(data), encoding="utf-8")
    res = subprocess.run([NODE, str(harness), str(WORKFLOW), str(scen)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 3
    assert "would check 0 claims while 2 high-risk claims" in res.stderr and "clear the verdicts" in res.stderr
    data["prepare"] = {"tier": 3, "preset": 0, "questions": []}
    out = run_workflow(tmp_path, data)
    assert "verify.json" in [Path(f["path"]).name for f in out["files"]]
    assert "preset" in out["calls"][1]["prompt"] and "evidence/review/offer.md/<the id of the row>.md" in \
        out["calls"][1]["prompt"]


# --------------------------------------------------------------------------- RV2, the workflows


@needs_node
def test_rv2_after_fx_runs_only_the_platform_lenses_and_the_partner_lens(project, tmp_path):  # noqa: F811
    data = scenario(project)
    data["args"]["deliverable"] = "deliverables/offer.final.md"
    data["args"]["after_fx"] = True
    out = run_workflow(tmp_path, data)
    agents = [c for c in out["calls"] if c["kind"] == "agent"]
    assert not [c for c in agents if c["phase"] == "Blind verification"]
    names = sorted(Path(f["path"]).name for f in out["files"])
    assert names == ["partner.json", "platform-azure.json", "platform-tcp.json"]
    lens_prompts = [c["prompt"] for c in agents if c["phase"] == "Lenses"]
    assert lens_prompts and all("there is no blind verification in this run" in p for p in lens_prompts)
    first = json.loads(out["files"][0]["content"]) if out["files"] else {}
    assert sum(1 for f in out["files"] if '"run"' in f["content"]) == 1 and first
    assert "final check stands in" in out["result"]["next"]
    data["args"]["deliverable"] = "deliverables/offer.md"
    scen = tmp_path / "scenario.json"
    from tests.test_review import HARNESS
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    scen.write_text(json.dumps(data), encoding="utf-8")
    res = subprocess.run([NODE, str(tmp_path / "harness.js"), str(WORKFLOW), str(scen)], capture_output=True,
                         text=True, timeout=60)
    assert res.returncode == 3 and "after_fx is only for the final text" in res.stderr


@needs_node
def test_rv2_the_final_check_records_the_hash_of_the_final_text(project, tmp_path):  # noqa: F811
    args = {"project": str(project), "deliverable": "deliverables/" + DRAFT, "date": "2026-10-27"}
    digest = "ab" * 32
    out = run_final(tmp_path, args, dict(RESULT, final_sha256=digest))
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    prompt = [c for c in data["calls"] if c["kind"] == "agent"][0]["prompt"]
    assert "sha256sum " + str(project / "deliverables" / FINAL) in prompt
    assert json.loads(data["files"][0]["content"])["final_sha256"] == digest
    assert "after_fx: true" in data["result"]["next"]
    out = run_final(tmp_path, args, dict(RESULT, final_sha256="not a hash"))
    data = json.loads(out.stdout)
    assert json.loads(data["files"][0]["content"])["final_sha256"] == ""
    assert "full review" in data["result"]["next"]


# --------------------------------------------------------------------------- RV2, the pass and the send gate


def test_rv2_a_matching_hash_exempts_the_final_text_and_the_send_gate_takes_the_final_check(
        home, register_path, tmp_path, capsys, fake):  # noqa: F811
    folder = spawn(home, register_path)
    final = start(folder, tmp_path, FINAL)
    fx_record(folder, final)
    fx = review.fx_exemption(folder, FINAL)
    assert fx.exempt and fx.record == "reviews/%s/final-check.json" % DRAFT
    lenses(folder, FINAL, [("platform-tcp", PLATFORM_LENS), ("partner", PARTNER_LENS)])
    res = review.run_pass(final)
    assert res.passed, res.problems
    assert res.record["final_check"] == fx.record and res.final_check == fx.record
    key, err, warn = Put(home, folder.name, capsys, fake)(final)
    assert key and not err and not warn
    # an empty verify lens beside it does not matter: the final check did that work
    lenses(folder, FINAL, [("verify", EMPTY_VERIFY)])
    assert review.run_pass(final).passed


def test_rv2_an_edit_after_the_final_check_brings_the_full_review_back(home, register_path, tmp_path, capsys,
                                                                       fake):  # noqa: F811
    folder = spawn(home, register_path)
    final = start(folder, tmp_path, FINAL)
    fx_record(folder, final)
    final.write_text(TEXT + "It fits the plan.\n", encoding="utf-8")
    review.write_claims(final)
    fill_all(folder, FINAL)
    fx = review.fx_exemption(folder, FINAL)
    assert not fx.exempt and fx.reason == review.FX_CHANGED
    lenses(folder, FINAL, [("platform-tcp", PLATFORM_LENS), ("partner", PARTNER_LENS)])
    res = review.run_pass(final)
    assert not res.passed
    assert any(p.startswith("the final text changed after the final check: the full review applies again")
               for p in res.problems)
    claim = review.load_claims(review.review_dir(folder, FINAL) / review.CLAIMS)[0].id
    lenses(folder, FINAL, [("verify", {"kind": "verify", "findings": [],
                                       "claims": [{"claim": claim, "verdict": "supported"}]})])
    assert review.run_pass(final).passed
    key, err, _ = Put(home, folder.name, capsys, fake)(final)
    assert key is None and "request check has not run" in err
    assert "the final text changed after the final check, so the full review applies again" in err
    lenses(folder, FINAL, [("request-check", REQUEST_LENS)])
    assert review.run_pass(final).passed
    key, err, _ = Put(home, folder.name, capsys, fake)(final)
    assert key and not err


def test_rv2_an_old_record_without_a_hash_gives_no_exemption(home, register_path, tmp_path, capsys,
                                                             fake):  # noqa: F811
    folder = spawn(home, register_path)
    final = start(folder, tmp_path, FINAL)
    fx_record(folder, None)
    fx = review.fx_exemption(folder, FINAL)
    assert not fx.exempt and fx.reason == review.FX_NO_HASH
    lenses(folder, FINAL, [("platform-tcp", PLATFORM_LENS), ("partner", PARTNER_LENS)])
    res = review.run_pass(final)
    assert not res.passed and any(p.startswith(review.FX_NO_HASH) for p in res.problems)
    # a record with a hash of another final text or of another kind is no final check of this one
    fx_record(folder, final, final="deliverables/other.final.md")
    assert review.fx_exemption(folder, FINAL) == review.FxState(False)
    fx_record(folder, final, kind="request")
    assert review.fx_exemption(folder, FINAL) == review.FxState(False)


def test_rv2_the_default_path_is_unchanged(home, register_path, tmp_path, capsys, fake):  # noqa: F811
    folder = spawn(home, register_path)
    full = reviewed(folder, tmp_path, 3, name=DRAFT, lenses=[("platform-tcp", PLATFORM_LENS)])
    final = start(folder, tmp_path, FINAL)
    fx_record(folder, final)
    assert review.fx_exemption(folder, DRAFT) == review.FxState(False)     # the draft is no final text
    key, err, _ = Put(home, folder.name, capsys, fake)(full)
    assert key is None and err.endswith("the request check has not run for this deliverable "
                                        "(workflows/request-check.js)")


# --------------------------------------------------------------------------- T6, the scriptPath and the guard


def _script_paths() -> list[str]:
    return re.findall(r"scriptPath `([^`]+)`", projects.FINAL_CHECK_SECTION)


def test_t6_the_template_names_the_installed_release_and_the_guard_accepts_exactly_that(tmp_path):
    paths = _script_paths()
    assert paths == ["/opt/tcp-awb/src/workflows/final-check.js", "/opt/tcp-awb/src/workflows/review.js"]
    assert projects.WORKFLOWS_INSTALLED == str(deploy.OPT / "src" / "workflows")
    # a fake /opt: releases/<commit>/workflows and the link src -> releases/<commit>, as a deploy lays it out
    opt = tmp_path / deploy.OPT.relative_to("/")
    release = opt / "releases" / ("c0" * 20)
    (release / "workflows").mkdir(parents=True)
    for name in toolguard.WORKFLOW_FILES:
        (release / "workflows" / name).write_text("// a workflow\n", encoding="utf-8")
    (opt / "src").symlink_to(Path("releases") / release.name)
    own = release / "workflows"
    for path in paths:
        fake_path = str(tmp_path) + path
        assert toolguard._workflow_class({"scriptPath": fake_path}, tmp_path, own) is None, path
    old = str(tmp_path) + "/opt/tcp-awb/workflows/final-check.js"
    assert toolguard._workflow_class({"scriptPath": old}, tmp_path, own) == toolguard.FOREIGN_WORKFLOW
    # the guard's own folder is the workflows folder beside the installed code, the one the link reaches
    assert toolguard.WORKFLOW_DIR == Path(toolguard.__file__).resolve().parent.parent / "workflows"


def test_t6_rv2_the_sync_moves_an_older_fx_section_to_the_new_path_and_adds_the_review_line(tmp_path):
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    new = projects._claude("tcp-q7m4", "project", projects.NO_CUSTOMER)
    old_line = projects.FX_SCRIPT_LINE.replace("/opt/tcp-awb/src/workflows/", "/opt/tcp-awb/workflows/")
    older = new.replace(projects.FX_SCRIPT_LINE, old_line).replace(projects.FX_REVIEW_LINE + "\n", "")
    assert "/opt/tcp-awb/workflows/" in older and "after_fx" not in older
    (root / "CLAUDE.md").write_text(older, encoding="utf-8")
    assert not rulesync.current(root)
    assert rulesync.sync_project(root) == ["CLAUDE.md"]
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == new
    assert rulesync.sync_project(root) == [] and rulesync.current(root)
    assert (old_line, projects.FX_SCRIPT_LINE) in rulesync.CLAUDE_FIXES
    assert any(add[1] == projects.FX_REVIEW_LINE for add in rulesync.CLAUDE_ADD)


def test_rv2_a_final_text_can_be_reviewed_and_a_name_in_its_stem_is_still_refused(home, register_path, tmp_path):
    from tests import fixtures

    folder = spawn(home, register_path)
    assert review._checked_path(FINAL) == "deliverables/" + DRAFT
    final = start(folder, tmp_path, FINAL, tier=1)
    assert review.l0(final).problems == []
    bad = folder / "deliverables" / ("offer-%s.final.md" % fixtures.CUSTOMER_FORMS[1].lower())
    bad.write_text(TEXT, encoding="utf-8")
    req = tmp_path / "request.txt"
    try:
        review.init(bad, req, 1, 400)
        refused = False
    except review.Refused:
        refused = True
    assert refused
