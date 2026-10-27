"""FX of 2026-10-27: the final check, a pass on a strong model that a project session runs on his phrase only.

workflows/final-check.js runs one agent pinned to Fable 5.1 at effort max that writes the answer again from the
request into deliverables/<stem>.final.md and the table into reviews/<name>/final-check.md; the script records the
run in reviews/<name>/final-check.json, which `awb review show` lists and the send gate never asks for. The tool
guard passes the workflow by its scriptPath like review.js and request-check.js (PD3). The project template carries
the phrase and the scriptPath; `awb projects sync` adds the section to an older project. Invented codes only.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from awb import gate, projects, review, rulesync, toolguard
from tests import fixtures
from tests.test_pd23_guard import call, world  # noqa: F401
from tests.test_review import NODE, needs_node, project, run  # noqa: F401
from tests.test_rulesync import old_project

ROOT = Path(__file__).resolve().parent.parent
FINAL = ROOT / "workflows" / "final-check.js"
PHASES = ["Final check", "Record"]
NAME = "tcp-q7m4-answer-en.md"

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
  calls.push({ kind: 'agent', phase: ph, prompt: prompt, model: opts.model || null, effort: opts.effort || null })
  if (ph === 'Final check') return scenario.result
  if (ph === 'Record') {
    const path = prompt.match(/^path: (.*)$/m)[1]
    const content = prompt.split('----- begin -----\n')[1].split('----- end -----')[0]
    files.push({ path: path, content: content })
    return { path: path, same: true }
  }
  throw new Error('unexpected agent call in phase ' + ph)
}
async function parallel(thunks) { return Promise.all(thunks.map((t) => t())) }
async function pipeline() { throw new Error('no pipeline here') }
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', 'budget', src)
body(scenario.args, agent, parallel, pipeline, phase, log, { total: null, spent: () => 0, remaining: () => Infinity })
  .then((result) => process.stdout.write(JSON.stringify({ result: result, calls: calls, files: files })))
  .catch((err) => { process.stderr.write(String((err && err.stack) || err)); process.exit(3) })
"""

RESULT = {"final_written": True, "table_written": True, "draft_bytes": 4200, "final_bytes": 3100, "questions": 4,
          "answered": 4, "dropped_by_contract": ["Which region holds the backup copy?"], "invented_by_contract": [],
          "caveats": 3, "unsupported_claims": 1, "open_points": ["the quota of the test tenant is not known"]}


def run_final(tmp_path: Path, args, result=RESULT) -> subprocess.CompletedProcess:
    harness = tmp_path / "fx-harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    scen = tmp_path / "fx-scenario.json"
    scen.write_text(json.dumps({"args": args, "result": result}), encoding="utf-8")
    return subprocess.run([NODE, str(harness), str(FINAL), str(scen)], capture_output=True, text=True, timeout=60)


def meta_block() -> str:
    src = FINAL.read_text(encoding="utf-8")
    return src[len("export const meta = "):src.index("\n}\n") + 2]


# --------------------------------------------------------------------------- the workflow


@needs_node
def test_the_final_check_passes_node_check_and_its_meta_is_a_pure_literal(tmp_path):
    src = FINAL.read_text(encoding="utf-8")
    assert src.startswith("export const meta = {")
    wrapper = tmp_path / "fx.js"
    wrapper.write_text("(async () => {\n" + src.replace("export const meta", "const meta", 1) + "\n})()\n",
                       encoding="utf-8")
    assert subprocess.run([NODE, "--check", str(wrapper)], capture_output=True, timeout=60).returncode == 0
    block = meta_block()
    for bad in ("`", "${", "...", "("):
        assert bad not in block, "the meta block is not a pure literal"
    probe = tmp_path / "meta.js"
    probe.write_text("const vm = require('vm')\nprocess.stdout.write(JSON.stringify(vm.runInNewContext('(' + "
                     + json.dumps(block) + " + ')', {})))\n", encoding="utf-8")
    res = subprocess.run([NODE, str(probe)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    assert meta["name"] == "awb-final-check" and [p["title"] for p in meta["phases"]] == PHASES
    for title in PHASES:
        assert "phase('%s')" % title in src


@needs_node
def test_the_final_check_runs_one_agent_pinned_to_fable_at_effort_max_and_records_the_run(project, tmp_path):
    out = run_final(tmp_path, {"project": str(project), "deliverable": str(project / "deliverables" / NAME),
                               "date": "2026-10-27"})
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    agents = [c for c in data["calls"] if c["kind"] == "agent"]
    assert [(a["phase"], a["model"], a["effort"]) for a in agents] == [("Final check", "fable", "max"),
                                                                        ("Record", None, "low")]
    prompt = agents[0]["prompt"]
    for must in ("/input/", "SCOPE.md", "STATE.md", "OPEN.md", "/evidence/", "contract.md", "claims.tsv",
                 "lenses/*.json", "before-voice.md", "verbatim", "never by patching the draft",
                 "customer's order", "I did not test X", "never longer than the draft", "First person singular",
                 "No em-dash", "T Cloud Public (TCP)", "Never the word Huawei", "Never change any other file"):
        assert must in prompt, must
    final = "deliverables/tcp-q7m4-answer-en.final.md"
    assert str(project / final) in prompt and str(project / "reviews" / NAME / "final-check.md") in prompt
    assert [f["path"] for f in data["files"]] == [str(project / "reviews" / NAME / "final-check.json")]
    record = json.loads(data["files"][0]["content"])
    assert record["kind"] == "final-check" and record["model"] == "Fable 5.1" and record["effort"] == "max"
    assert record["deliverable"] == "deliverables/" + NAME and record["final"] == final
    assert record["date"] == "2026-10-27" and record["dropped"] == RESULT["dropped_by_contract"]
    assert data["result"]["final"] == final and data["result"]["written"] is True
    assert data["result"]["open_points"] == RESULT["open_points"]


@needs_node
def test_the_final_check_refuses_a_path_outside_deliverables_or_a_final_text(project, tmp_path):
    for deliverable in ("evidence/x.md", "deliverables/../SCOPE.md", "deliverables/tcp-q7m4-answer-en.final.md"):
        out = run_final(tmp_path, {"project": str(project), "deliverable": deliverable, "date": "2026-10-27"})
        assert out.returncode == 3, deliverable
    out = run_final(tmp_path, {"project": "relative", "deliverable": "deliverables/" + NAME})
    assert out.returncode == 3


# --------------------------------------------------------------------------- the tool guard (PD3)


def test_the_guard_runs_the_final_check_by_its_script_path(world):  # noqa: F811
    path = toolguard.WORKFLOW_DIR / "final-check.js"
    assert path.is_file() and "final-check.js" in toolguard.WORKFLOW_FILES
    assert set(toolguard.WORKFLOW_FILES) == {"review.js", "request-check.js", "final-check.js"}
    assert call(world, "Workflow", scriptPath=str(path), args={"project": str(world["own"])}) is None
    assert call(world, "Workflow", scriptPath=str(path), resumeFromRunId="r-1") is None


def test_the_guard_still_refuses_an_inline_script_and_any_other_path(world):  # noqa: F811
    copy = world["own"] / "final-check.js"
    copy.write_text(FINAL.read_text(encoding="utf-8"), encoding="utf-8")
    script = "export const meta = {name: 'x', description: 'x'}\nawait workflow({scriptPath: '%s'})" % copy
    for tool_input in ({"script": script}, {"name": "awb-final-check"}, {"scriptPath": str(copy)},
                       {"scriptPath": str(toolguard.WORKFLOW_DIR / "refresh-brief.md")},
                       {"scriptPath": str(toolguard.WORKFLOW_DIR / "other-check.js")},
                       {"scriptPath": str(toolguard.WORKFLOW_DIR / "final-check.js"), "script": script}):
        assert call(world, "Workflow", **tool_input) == toolguard.FOREIGN_WORKFLOW, tool_input


# --------------------------------------------------------------------------- the project template


def test_the_template_carries_the_phrase_and_the_script_path(home, register_path):
    from tests.test_lifecycle import spawn

    pr = spawn(home, register_path)
    text = (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "## Final check (FX)" in text
    assert "\"финальная вычитка\"" in text and "\"final check\"" in text
    assert "Fable 5.1 at effort max" in text and "200k tokens" in text and "Wait for one word" in text
    assert "never on your own and never as part of another step" in text
    assert "\"Final check (FX, DATE)\"" in text and "OPEN.md" in text
    assert len(text.splitlines()) <= projects.MAX_CLAUDE_LINES
    assert rulesync.sync_project(Path(pr.path)) == []
    assert gate.scan_files([Path(pr.path) / "CLAUDE.md"], None) == []


def test_the_sync_adds_the_final_check_section_to_an_older_project_once(tmp_path):
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    older = projects._claude("tcp-q7m4", "project", projects.NO_CUSTOMER).replace(
        "\n" + projects.FINAL_CHECK_SECTION, "") + "- A note of this project: the lab image lives in the lab bucket.\n"
    assert "final-check.js" not in older
    (root / "CLAUDE.md").write_text(older, encoding="utf-8")
    assert not rulesync.current(root)
    assert rulesync.sync_project(root) == ["CLAUDE.md"]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.count("## Final check (FX)") == 1 and "workflows/final-check.js" in text
    assert text.endswith(projects.FINAL_CHECK_SECTION)
    assert "- A note of this project: the lab image lives in the lab bucket." in text
    assert rulesync.sync_project(root) == [] and rulesync.current(root)


def test_a_project_file_without_the_template_lines_gets_no_section(tmp_path):
    root = old_project(tmp_path)
    rulesync.sync_project(root)
    assert "final-check.js" not in (root / "CLAUDE.md").read_text(encoding="utf-8")
    (root / "CLAUDE.md").write_text("# Project tcp-q7m4\n\n- Codes only.\n", encoding="utf-8")
    assert rulesync.sync_project(root) == [] and rulesync.current(root)


# --------------------------------------------------------------------------- awb review show


def write_record(project: Path, **over) -> Path:
    folder = project / "reviews" / NAME
    folder.mkdir(parents=True, exist_ok=True)
    data = {"kind": "final-check", "deliverable": "deliverables/" + NAME, "date": "2026-10-27",
            "model": "Fable 5.1", "effort": "max", "final": "deliverables/tcp-q7m4-answer-en.final.md",
            "open_points": ["one", "two"]}
    data.update(over)
    path = folder / review.FINAL_CHECK
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_review_show_lists_the_final_check_when_there_is_one(project, capsys):  # noqa: F811
    (project / "deliverables" / NAME).write_text("A draft for CUST-Q7M4.\n", encoding="utf-8")
    code, out, _ = run(["review", "show", str(project / "deliverables" / NAME)], capsys)
    assert code == 0 and out.splitlines() == ["deliverables/%s: record missing" % NAME, "final check: none"]
    write_record(project)
    fc = review.final_check(project, NAME)
    assert fc is not None and fc["model"] == "Fable 5.1" and fc["open_points"] == 2
    code, out, err = run(["review", "show", str(project / "deliverables" / NAME)], capsys)
    assert code == 0
    assert out.splitlines()[1] == ("final check: 2026-10-27, Fable 5.1 effort max, "
                                   "deliverables/tcp-q7m4-answer-en.final.md; 2 open points in "
                                   "reviews/%s/final-check.md" % NAME)
    fixtures.assert_no_fixture_name(out + err, "the output of review show")


def test_the_final_name_is_the_stem_of_the_draft_with_final_md():
    assert review.final_name(NAME) == "deliverables/tcp-q7m4-answer-en.final.md"
    assert review.final_name("sub/notes") == "deliverables/sub/notes.final.md"
    assert review.final_name("sub/.plan.md") == "deliverables/sub/.plan.final.md"
    assert 'final: \'deliverables/\' + stem + \'.final.md\'' in FINAL.read_text(encoding="utf-8")


def test_a_record_of_another_deliverable_or_another_kind_is_no_final_check(project):  # noqa: F811
    write_record(project, deliverable="deliverables/other.md")
    assert review.final_check(project, NAME) is None
    write_record(project, kind="request")
    assert review.final_check(project, NAME) is None
    write_record(project, final="evidence/x.md", model="a model; rm -rf")
    fc = review.final_check(project, NAME)
    assert fc is not None and fc["final"] == "" and fc["model"] == ""


def test_the_final_check_binds_nothing_and_the_status_ignores_it(project):  # noqa: F811
    folder = project / "reviews" / NAME
    before = review.bindings(folder)
    write_record(project)
    assert review.bindings(folder) == before
