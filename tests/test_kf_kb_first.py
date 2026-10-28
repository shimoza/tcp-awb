"""Knowledge base first (task KF, awb/kbfirst.py): the prompt hook shows the closest facts once per task, the work
rules ask for them before a new task or a run, the final check and the review claims say what was known before.

2026-10-28: a session spent a 35-minute live run on an OBS 403 with federated temporary AK/SK; facts of other
projects in the knowledge base said that rights granted through IAM took 20 to 30 minutes to work while a bucket
policy worked in seconds.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from awb import kb, kbfirst, projects, review, rulesync
from tests.test_hooks import run_hook
from tests.test_lifecycle import spawn
from tests.test_rulesync import old_project

TODAY = datetime.date.today()
EARLIER = (TODAY - datetime.timedelta(days=20)).isoformat()
OBS_DELAY = ("OBS rights granted through an IAM policy to temporary AK/SK of a federated identity took 20 to 30 "
             "minutes to work: the bucket list answered 403 until then, while a bucket policy worked in seconds.")
OBS_SIGNER = "The OBS V2 signer with temporary AK/SK needs the security token header, else the bucket answers 403."
ECS_FLAVOR = "The flavor s3.large.2 is offered in eu-de for general purpose machines."
RUN_PROMPT = "прогони тест: временные AK/SK федеративного пользователя, OBS отвечает 403 на bucket list"


def add_entry(home, statement: str, tag: str = "obs", checked: str = EARLIER) -> str:
    return kb.add(statement, scope="tcp", tags=[tag], grade="live", cls="stable", source="an invented lab run",
                  checked=checked, force_new=True, where=home, register_path=home.register, today=TODAY).id


def context(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


# --------------------------------------------------------------------------- the prompt hook


def test_the_first_prompt_and_a_run_prompt_get_the_block_and_an_ordinary_prompt_does_not(home, register_path,
                                                                                          monkeypatch, capsys):
    delay = add_entry(home, OBS_DELAY)
    add_entry(home, ECS_FLAVOR, tag="ecs")
    pr = spawn(home, register_path)
    payload = {"cwd": pr.path, "session_id": "kf-session"}
    run_hook("session-start", payload, monkeypatch, capsys)
    code, out, _ = run_hook("prompt", dict(payload, prompt="the bucket list of OBS answers 403 with temporary AK/SK"),
                            monkeypatch, capsys)
    first = context(out)
    assert code == 0 and first.startswith("Known from the knowledge base (")
    assert delay in first and "20 to 30" in first
    # the next ordinary prompt: no block
    assert run_hook("prompt", dict(payload, prompt="ok, commit that and update the state file"),
                    monkeypatch, capsys) == (0, "", "")
    # a run prompt on facts already told: nothing new to say
    assert run_hook("prompt", dict(payload, prompt=RUN_PROMPT), monkeypatch, capsys) == (0, "", "")
    # a run prompt on a new topic: the block again
    signer = add_entry(home, OBS_SIGNER)
    code, out, _ = run_hook("prompt", dict(payload, prompt=RUN_PROMPT), monkeypatch, capsys)
    assert code == 0 and signer in context(out)


def test_a_run_prompt_in_a_new_session_lists_the_obs_delay_fact_first(home, register_path):
    delay = add_entry(home, OBS_DELAY)
    add_entry(home, ECS_FLAVOR, tag="ecs")
    root = Path(spawn(home, register_path).path)
    said = kbfirst.block(home, "kf-run", root, RUN_PROMPT)
    lines = said.splitlines()
    assert lines[0] == kbfirst.HEAD % 1
    assert lines[1].startswith("%s live %s: " % (delay, EARLIER))
    assert len(said) <= kbfirst.MAX_CHARS


def test_the_block_stays_within_its_size_and_after_two_hours_a_prompt_starts_a_new_task(home, register_path):
    for n in range(14):
        add_entry(home, "OBS bucket test %d of temporary AK/SK answered 403 in the invented lab run number %d."
                  % (n, n))
    root = Path(spawn(home, register_path).path)
    said = kbfirst.block(home, "kf-size", root, RUN_PROMPT, now=1000.0)
    assert said and len(said) <= kbfirst.MAX_CHARS and len(said.splitlines()) <= kbfirst.MAX_FACTS + 1
    assert said.splitlines()[0] == kbfirst.HEAD % 14
    assert kbfirst.block(home, "kf-size", root, "fine, go on", now=1000.0 + 60) is None
    assert kbfirst.block(home, "kf-size", root, "OBS bucket 403 again", now=1060.0 + kbfirst.SILENCE + 1)


def test_a_knowledge_base_that_cannot_be_read_adds_nothing_and_blocks_nothing(home, register_path, monkeypatch,
                                                                             capsys):
    add_entry(home, OBS_DELAY)
    pr = spawn(home, register_path)

    def broken(*a, **kw):
        raise kb.KBError("the knowledge base cannot be read (PermissionError)")

    monkeypatch.setattr(kb, "find", broken)
    assert kbfirst.block(home, "kf-broken", Path(pr.path), RUN_PROMPT) is None
    payload = {"cwd": pr.path, "session_id": "kf-broken-hook"}
    run_hook("session-start", payload, monkeypatch, capsys)
    assert run_hook("prompt", dict(payload, prompt=RUN_PROMPT), monkeypatch, capsys) == (0, "", "")


def test_an_empty_base_and_a_short_prompt_add_nothing(home, register_path):
    root = Path(spawn(home, register_path).path)
    assert kbfirst.block(home, "kf-empty", root, RUN_PROMPT) is None
    add_entry(home, OBS_DELAY)
    assert kbfirst.block(home, "kf-short", root, "ok go") is None


def test_the_rules_notice_keeps_the_facts_told(home, register_path, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    add_entry(home, OBS_DELAY)
    root = Path(spawn(home, register_path).path)
    assert kbfirst.block(home, "kf-keep", root, RUN_PROMPT)
    rulesync.remember(home, "kf-keep", root)
    assert kbfirst.block(home, "kf-keep", root, RUN_PROMPT) is None


# --------------------------------------------------------------------------- the rules


def test_the_template_and_the_sync_carry_the_kb_first_line_once(home, register_path, tmp_path):
    pr = spawn(home, register_path)
    text = (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.count(projects.KB_FIRST) == 1 and text.count("\n") <= projects.MAX_CLAUDE_LINES
    assert rulesync.sync_project(Path(pr.path)) == []
    root = old_project(tmp_path)
    rulesync.sync_project(root)
    rulesync.sync_project(root)
    assert (root / "CLAUDE.md").read_text(encoding="utf-8").count(projects.KB_FIRST) == 1


def test_the_work_rules_ask_for_the_base_first():
    rules = (Path(__file__).resolve().parent.parent / "seal" / "work-claude" / "CLAUDE.md").read_text(encoding="utf-8")
    flat = " ".join(rules.split())
    assert "Knowledge base first: before a new task or a run" in flat and "tell him first" in flat


# --------------------------------------------------------------------------- final check and review claims


def test_the_final_check_prompt_asks_for_the_known_before_column():
    src = (Path(__file__).resolve().parent.parent / "workflows" / "final-check.js").read_text(encoding="utf-8")
    assert src.count("known from the base before the work (yes/no") == 2 and "kb.tsv" in src


def test_the_review_claims_say_which_claim_was_known_before(home, register_path, tmp_path):
    from tests.test_review_board import REQUEST, TEXT

    add_entry(home, "The flavor s3.large.2 runs 3 nodes per cluster in eu-de in the invented test.", tag="ecs")
    for other in (OBS_DELAY, OBS_SIGNER, "A snapshot of an EVS disk is kept until it is deleted by hand.",
                  "The price API answers in JSON with one row per region and service."):
        add_entry(home, other)
    folder = Path(spawn(home, register_path).path)
    d = folder / "deliverables" / "reply.md"
    d.write_text(TEXT, encoding="utf-8")
    req = tmp_path / "request.txt"
    req.write_text(REQUEST, encoding="utf-8")
    review.init(d, req, 2, 400)
    res = review.write_claims(d)
    table = res.kb_path.read_text(encoding="utf-8").splitlines()
    assert table[0] == "id\trisk\tentries\tknown_before"
    assert all(row.split("\t")[3] in ("yes", "no") for row in table[1:])
    assert table[1].split("\t")[3] == "yes" and res.known_before >= 1
    assert not review.known_before([{"score": 0.9, "checked": TODAY.isoformat()}], TODAY.isoformat())
    assert not review.known_before([{"score": 0.2, "checked": EARLIER}], TODAY.isoformat())
