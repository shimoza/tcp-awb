# TCP Facts evaluation set

A number for how much the dataset TCP Facts helps a model: 50 questions a cloud architect would ask, each with
the expected answer, the fact ids that support it and a grading rule. The same questions are asked twice, with
`facts.md` attached and without. The two scores are compared.

The set is built from the release `2026-10-12` of github.com/shimoza/tcp-facts (`facts.jsonl`, `services.md`,
`prices/*.csv`) and from nothing else. It holds no customer, project or tenant. A new release makes some answers
stale: rebuild the set against it before you trust a score (see "Keeping it current").

## Files

| file | what it is |
|---|---|
| `questions.jsonl` | the 50 questions, one JSON object per line |
| `score.py` | grades a file of answers and prints the score per category (standard library) |
| `run.py` | asks a model every question with and without the facts (standard library, needs `ANTHROPIC_API_KEY`) |

## The questions

| category | count | about |
|---|---|---|
| availability | 10 | what is offered in eu-de and eu-nl, versions, AZs |
| limits | 10 | quotas and retention |
| api | 10 | how an API call or a service behaves, error codes |
| scope | 5 | the service description (`services.md`) |
| price | 7 | list prices of the price snapshot |
| negative | 8 | what TCP does not offer or does not do, each with its evidence |

Fields of a question: `id`, `category`, `question`, `answer_form`, `expected`, `fact_ids` (the supporting facts of
the release; empty when the support is `services.md` or a price row, named in `source`), `grading`, `release`.
A price question also has `snapshot_bound: true`, the `snapshot` date (2026-10-08), `unit` and `currency`: the live
price API is the source of truth, so a price answer is right for that date only. A negative question has
`evidence`: the checks the fact records (`tried`).

## Grading rules

| type | right when |
|---|---|
| `yesno` | the answer says the expected yes or no |
| `number` | the first number of the answer (fact ids and dates left out; `2,500,000` reads as one number) equals the expected one within `tolerance` |
| `list` | every expected name appears as a whole word and no `forbidden` one does |
| `citation` | the answer names one of the fact ids of the rule |

"No checked fact covers this" is wrong and is counted in its own column (abstained). The column cited is the share
of answers that name a supporting fact id. A question without an answer is wrong.

## How to run it

```bash
# 1. get the files of the release
git clone --depth 1 https://github.com/shimoza/tcp-facts /tmp/tcp-facts

# 2. ask a model each question twice (this calls the API and costs credit)
export ANTHROPIC_API_KEY=...        # the key stays in your shell, never in a file
python3 eval/tcp-facts/run.py --facts /tmp/tcp-facts/facts.md --out /tmp/eval-out
#   --model ID  another model (default claude-sonnet-5-5)      --limit N  only the first N questions

# 3. grade both files
python3 eval/tcp-facts/score.py /tmp/eval-out/answers-without-facts.jsonl
python3 eval/tcp-facts/score.py /tmp/eval-out/answers-with-facts.jsonl
```

Without `ANTHROPIC_API_KEY`, `run.py` prints that it did nothing and exits 0. `score.py` takes any file of lines
`{"id": "Q01", "answer": "text"}`, so you can grade answers from any other source. `--json` prints the figures as
JSON.

The score of the answers without the facts shows what the model knows alone and the score with them what the
dataset adds. Read the price category apart: a model without the snapshot has no way to know a 2026-10-08 price.

## Keeping it current

The facts of the set are checked against the release when `questions.jsonl` is built: a fact id must exist and its
statement must say what the expected answer says. When a new release changes or drops a fact, re-check that
question and its `release` field. Quota and availability facts carry a check date and an expiry in the release; a
question about a fact that has expired should be replaced.

## Tests

`.venv/bin/pytest tests/test_eval_tcp_facts.py` checks the set (50 questions, fields, snapshot marks, evidence) and
`score.py` with invented answer files. It calls no model.
