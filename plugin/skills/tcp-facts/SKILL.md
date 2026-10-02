---
name: tcp-facts
description: Answer a question about T Cloud Public (TCP) services, limits, flavors, regions or APIs from checked facts, never from memory
---

# Facts about TCP

Answer from the knowledge base of the Architect Workbench, never from memory.

1. Search it: `awb kb find WORDS...` (a few key words, such as `cce node flavors`). Read an entry in full with
   `awb kb show KB-XXXX`.
2. Every entry has a grade. `live` was checked on the platform with a live call, `contract` comes from the service
   description, `docs` from the documentation. `said` is a statement from the platform team and `assumed` is not
   checked: both are leads, never proof on their own. Say which grade a fact has when it matters.
3. Name the entry of every fact you use: (KB-XXXX). An entry marked EXPIRED is a lead: check it again before you
   state it.
4. A negative ("this is not possible") needs two recorded attempts. Without them say that it is not confirmed.
5. When no entry answers the question, say so and say what would need to be checked (a live read call, the
   service description, the documentation). Never fill the gap with a guess.
6. What you checked yourself goes back in: `awb kb add --scope tcp --tag T --grade live|docs --class api|availability
   --source "where" "the fact"`, one fact per entry, codes only. The checks of `kb add` refuse names, prices and
   unproven negatives. Do it without asking him.
7. A team that wants the facts as data gets `awb kb export --out facts.jsonl` (live, contract and docs only).
