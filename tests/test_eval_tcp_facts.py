"""Tests of eval/tcp-facts: the question set, score.py (invented answer files) and run.py without a key."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parent.parent / "eval" / "tcp-facts"


def load(name):
    spec = importlib.util.spec_from_file_location("evalcase_" + name, EVAL / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


score = load("score")


@pytest.fixture(scope="module")
def questions():
    return score.load_jsonl(EVAL / "questions.jsonl")


def write_answers(path, answers):
    path.write_text("".join(json.dumps({"id": k, "answer": v}) + "\n" for k, v in answers.items()), encoding="utf-8")
    return path


def right_answer(q):
    """An answer that is right by the rule of the question."""
    t, e = q["grading"]["type"], q["expected"]
    if t == "list":
        return ", ".join(e)
    return "%s (%s)" % (e, q["fact_ids"][0]) if q.get("fact_ids") else str(e)


# --------------------------------------------------------------------------- the set

def test_the_set_has_fifty_questions_in_every_kind(questions):
    assert len(questions) == 50
    assert len({q["id"] for q in questions}) == 50
    assert {q["category"] for q in questions} == {"availability", "limits", "api", "scope", "price", "negative"}
    assert {q["grading"]["type"] for q in questions} == {"yesno", "number", "list", "citation"}


def test_every_question_has_its_answer_support_and_rule(questions):
    for q in questions:
        assert q["question"] and q["answer_form"] and q["release"] == "2026-10-12"
        assert q["fact_ids"] or q.get("source"), q["id"]
        assert all(re.fullmatch(r"KB-[A-Z0-9]{4}", i) for i in q["fact_ids"])
        assert q["expected"] not in (None, "", [])


def test_price_questions_are_snapshot_bound_with_the_date(questions):
    prices = [q for q in questions if q["category"] == "price"]
    assert prices
    for q in prices:
        assert q["snapshot_bound"] is True and q["snapshot"] == "2026-10-08"
        assert "2026-10-08" in q["question"]
    assert not any(q.get("snapshot_bound") for q in questions if q["category"] != "price")


def test_negative_questions_carry_their_evidence(questions):
    negatives = [q for q in questions if q["category"] == "negative"]
    assert negatives
    for q in negatives:
        assert q["evidence"] and all(e["tried"] for e in q["evidence"]), q["id"]


def test_the_expected_answer_of_every_question_grades_right(questions):
    for q in questions:
        assert score.grade(q, right_answer(q)), q["id"]


# --------------------------------------------------------------------------- the graders

def test_yes_no_is_read_from_the_answer_and_the_wrong_one_fails():
    q = {"grading": {"type": "yesno"}, "expected": "no"}
    assert score.grade(q, "No. It is not offered (KB-ABCD).")
    assert not score.grade(q, "Yes, it is.")
    assert not score.grade(q, "It might be.")


def test_number_ignores_fact_ids_and_dates_and_reads_thousands():
    q = {"grading": {"type": "number", "tolerance": 0}, "expected": 2500000}
    assert score.grade(q, "2,500,000 MB (KB-1234)")
    q = {"grading": {"type": "number", "tolerance": 0}, "expected": 100}
    assert score.grade(q, "On 2026-09-25 the limit was 100 (KB-9999)")
    assert not score.grade(q, "1000")
    assert not score.grade(q, "none")


def test_number_uses_the_tolerance():
    q = {"grading": {"type": "number", "tolerance": 0.000001}, "expected": 0.108287}
    assert score.grade(q, "EUR 0.108287 per hour")
    assert not score.grade(q, "EUR 0.11 per hour")


def test_list_needs_every_item_whole_and_no_forbidden_one():
    q = {"grading": {"type": "list", "items": ["4.0", "5.0", "6.0"], "forbidden": ["7.0"]}, "expected": []}
    assert score.grade(q, "4.0, 5.0 and 6.0")
    assert not score.grade(q, "4.0 and 5.0")
    assert not score.grade(q, "4.0, 5.0, 6.0 and 7.0")
    assert not score.grade(q, "14.0, 5.0, 6.0")


def test_citation_needs_one_of_the_ids():
    q = {"grading": {"type": "citation", "ids": ["KB-ABCD"]}, "expected": "KB-ABCD"}
    assert score.grade(q, "see (KB-ABCD)")
    assert not score.grade(q, "see (KB-ABCE)")


# --------------------------------------------------------------------------- the score

def test_the_score_is_counted_per_category(questions, tmp_path):
    answers = {q["id"]: right_answer(q) for q in questions}
    wrong = [q for q in questions if q["category"] == "limits"][:4]
    for q in wrong:
        answers[q["id"]] = "I do not know"
    cats = score.score(questions, {k: v for k, v in answers.items()})
    assert cats["limits"]["correct"] == 6 and cats["limits"]["total"] == 10
    assert cats["api"]["correct"] == 10
    assert score.totals(cats)["correct"] == 46


def test_an_abstention_is_wrong_and_counted_apart(questions):
    q = questions[0]
    cats = score.score([q], {q["id"]: "No checked fact covers this."})
    only = cats[q["category"]]
    assert only["correct"] == 0 and only["abstained"] == 1


def test_a_missing_answer_is_wrong(questions):
    cats = score.score(questions, {})
    assert score.totals(cats)["correct"] == 0 and score.totals(cats)["total"] == 50


def test_the_cited_share_counts_supporting_ids(questions):
    q = next(x for x in questions if x["fact_ids"])
    cats = score.score([q], {q["id"]: "yes (%s)" % q["fact_ids"][0]})
    assert cats[q["category"]]["cited"] == 1
    cats = score.score([q], {q["id"]: "yes"})
    assert cats[q["category"]]["cited"] == 0


def test_the_command_line_prints_a_table_per_category(questions, tmp_path, capsys):
    path = write_answers(tmp_path / "answers.jsonl", {q["id"]: right_answer(q) for q in questions})
    assert score.main([str(path)]) == 0
    out = capsys.readouterr().out
    for name in ("availability", "limits", "api", "scope", "price", "negative", "all"):
        assert re.search(r"^%s\s" % name, out, re.M)
    assert "score: 100.0%" in out


def test_the_json_output_has_the_figures(questions, tmp_path, capsys):
    path = write_answers(tmp_path / "answers.jsonl", {"Q01": "no", "Q99": "x"})
    assert score.main([str(path), "--json"]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["all"]["total"] == 50
    assert "Q99" in cap.err


def test_a_file_that_is_not_json_is_refused(tmp_path):
    bad = tmp_path / "answers.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        score.main([str(bad)])


# --------------------------------------------------------------------------- run.py

def test_run_without_a_key_does_nothing_and_says_so(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    out = tmp_path / "out"
    p = subprocess.run([sys.executable, str(EVAL / "run.py"), "--out", str(out)], env=env, capture_output=True, text=True)
    assert p.returncode == 0
    assert "ANTHROPIC_API_KEY is not set" in p.stdout
    assert not out.exists()
