#!/usr/bin/env python3
"""Ask a model every question of the TCP Facts set twice, with facts.md attached and without. Write both answer files.

    ANTHROPIC_API_KEY=... python3 run.py --facts PATH/facts.md [--model ID] [--out DIR] [--limit N]

Without ANTHROPIC_API_KEY it does nothing and says so. It writes DIR/answers-with-facts.jsonl and
DIR/answers-without-facts.jsonl (default DIR: the current directory) and then you grade each with score.py.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
RULE = ("Give the answer first, in one short line: yes or no, a number, a list of names or a fact id, as the question asks. "
        "A number has no unit and no thousands separator.")
SYSTEM_WITHOUT = "You are a cloud architect's assistant for T Cloud Public (TCP). " + RULE
SYSTEM_WITH = ("You are a cloud architect's assistant for T Cloud Public (TCP). Answer only from the facts below and put the "
               "id of every fact you use after the answer, like (KB-XXXX). " + RULE + "\n\n")


def ask(key: str, model: str, system: list[dict], question: str) -> str:
    body = json.dumps({"model": model, "max_tokens": 300, "system": system,
                       "messages": [{"role": "user", "content": question}]}).encode()
    req = urllib.request.Request(URL, body, {"x-api-key": key, "anthropic-version": "2023-06-01",
                                             "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit("the API answered HTTP %d" % e.code)
    return "".join(b.get("text", "") for b in data.get("content", []))


def question_text(q: dict) -> str:
    return "%s\n(Answer form: %s.)" % (q["question"], q["answer_form"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--facts", type=Path, help="facts.md of the release (required)")
    ap.add_argument("--questions", type=Path, default=HERE / "questions.jsonl")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", type=Path, default=Path("."))
    ap.add_argument("--limit", type=int, default=0, help="ask only the first N questions")
    args = ap.parse_args(argv)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("ANTHROPIC_API_KEY is not set: nothing was asked and nothing was written.")
        return 0
    if not args.facts or not args.facts.is_file():
        raise SystemExit("--facts must name the facts.md of the release")
    questions = [json.loads(l) for l in args.questions.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        questions = questions[:args.limit]
    # the facts go first in the system block and are cached: they are the same for all questions
    with_facts = [{"type": "text", "text": SYSTEM_WITH + args.facts.read_text(encoding="utf-8"),
                   "cache_control": {"type": "ephemeral"}}]
    without_facts = [{"type": "text", "text": SYSTEM_WITHOUT}]
    args.out.mkdir(parents=True, exist_ok=True)
    for name, system in (("answers-with-facts.jsonl", with_facts), ("answers-without-facts.jsonl", without_facts)):
        with open(args.out / name, "w", encoding="utf-8") as f:
            for q in questions:
                a = ask(key, args.model, system, question_text(q))
                f.write(json.dumps({"id": q["id"], "answer": a}, ensure_ascii=False) + "\n")
                f.flush()
        print("wrote %s" % (args.out / name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
