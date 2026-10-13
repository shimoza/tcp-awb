#!/usr/bin/env python3
"""Grade a file of model answers against the TCP Facts question set and print the score per category.

    python3 score.py ANSWERS.jsonl [--questions questions.jsonl] [--json]

ANSWERS.jsonl holds one JSON object per line: {"id": "Q01", "answer": "text"}. A question without an answer
counts as wrong. The grading rule of each question decides:

  yesno     the answer says yes or no and it is the expected one
  number    the first number of the answer (fact ids and dates left out) is the expected one, within the tolerance
  list      every expected item appears as a whole word and no forbidden item appears
  citation  the answer names one of the fact ids of the rule

An answer that says "no checked fact covers this" is an abstention: it is wrong and also counted apart.
The cited column is the share of answers that name at least one supporting fact id (questions with ids only).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABSTAIN = re.compile(r"no checked fact covers|no fact covers|cannot (?:answer|be answered) from", re.I)
FACT_ID = re.compile(r"\bKB-[A-Z0-9]{4}\b")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}\.\d{2}\.\d{4}\b")
NUMBER = re.compile(r"(?<![\w.])-?\d{1,3}(?:[,_]\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")
YESNO = re.compile(r"\b(yes|no)\b", re.I)


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                raise SystemExit("%s: line %d is not JSON" % (path, n))
    return rows


def abstained(answer: str) -> bool:
    return bool(ABSTAIN.search(answer))


def grade_yesno(answer: str, rule: dict, expected) -> bool:
    m = YESNO.search(FACT_ID.sub(" ", answer))
    return bool(m) and m.group(1).lower() == str(expected).lower()


def first_number(answer: str):
    text = DATE.sub(" ", FACT_ID.sub(" ", answer))
    m = NUMBER.search(text)
    return float(re.sub(r"[,_]", "", m.group(0))) if m else None


def grade_number(answer: str, rule: dict, expected) -> bool:
    got = first_number(answer)
    return got is not None and abs(got - float(expected)) <= float(rule.get("tolerance", 0)) + 1e-12


def has_item(answer: str, item: str) -> bool:
    return re.search(r"(?<![\w.])%s(?![\w]|\.\d)" % re.escape(item), answer, re.I) is not None


def grade_list(answer: str, rule: dict, expected) -> bool:
    items = rule.get("items", expected)
    if not all(has_item(answer, i) for i in items):
        return False
    return not any(has_item(answer, i) for i in rule.get("forbidden", []))


def grade_citation(answer: str, rule: dict, expected) -> bool:
    return any(i in set(FACT_ID.findall(answer)) for i in rule.get("ids", []))


GRADERS = {"yesno": grade_yesno, "number": grade_number, "list": grade_list, "citation": grade_citation}


def grade(question: dict, answer: str) -> bool:
    rule = question["grading"]
    return GRADERS[rule["type"]](answer, rule, question.get("expected"))


def score(questions: list[dict], answers: dict[str, str]) -> dict:
    cats: dict[str, dict] = {}
    for q in questions:
        c = cats.setdefault(q["category"], {"total": 0, "correct": 0, "abstained": 0, "cited": 0, "citable": 0})
        c["total"] += 1
        a = answers.get(q["id"], "")
        if abstained(a):
            c["abstained"] += 1
        elif a.strip() and grade(q, a):
            c["correct"] += 1
        if q.get("fact_ids"):
            c["citable"] += 1
            if set(q["fact_ids"]) & set(FACT_ID.findall(a)):
                c["cited"] += 1
    return cats


def totals(cats: dict) -> dict:
    return {k: sum(c[k] for c in cats.values()) for k in ("total", "correct", "abstained", "cited", "citable")}


def pct(a: int, b: int) -> str:
    return "%5.1f%%" % (100.0 * a / b) if b else "    -"


def render(cats: dict) -> str:
    lines = ["%-14s %7s %9s %10s %9s" % ("category", "correct", "of", "abstained", "cited")]
    for name in sorted(cats) + ["all"]:
        c = totals(cats) if name == "all" else cats[name]
        lines.append("%-14s %7d %9d %10d %9s" % (name, c["correct"], c["total"], c["abstained"], pct(c["cited"], c["citable"])))
    t = totals(cats)
    lines.append("score: %s" % pct(t["correct"], t["total"]).strip())
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("answers", type=Path)
    ap.add_argument("--questions", type=Path, default=HERE / "questions.jsonl")
    ap.add_argument("--json", action="store_true", help="print the figures as JSON")
    args = ap.parse_args(argv)
    questions = load_jsonl(args.questions)
    answers = {r["id"]: str(r.get("answer", "")) for r in load_jsonl(args.answers) if "id" in r}
    unknown = sorted(set(answers) - {q["id"] for q in questions})
    if unknown:
        print("answers for unknown questions ignored: %s" % ", ".join(unknown), file=sys.stderr)
    cats = score(questions, answers)
    print(json.dumps({"categories": cats, "all": totals(cats)}, indent=1) if args.json else render(cats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
