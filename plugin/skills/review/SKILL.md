---
name: review
description: Review a deliverable (architecture, migration concept, offer text, customer answer) before it leaves the project
---

# Review before a deliverable leaves

A deliverable lives under `deliverables/` of a Workbench project. It leaves only with a valid review record.

1. Set the contract first: `awb review init deliverables/FILE --request REQUEST.md --tier N --budget WORDS`. A text
   for a customer or a partner is tier 3. The budget is set from the request and does not grow later.
2. List every checkable statement with its evidence: `awb review claims deliverables/FILE`, then fill evidence and
   verdict in `reviews/FILE/claims.tsv`. Facts come from `awb kb find`, prices from `awb price`. A computed number
   (a total, a sum, a saving, 20 x 8) needs its calculation: run `awb calc` and give `calc:K-N` as evidence.
3. Run the level-0 checks: `awb review l0 deliverables/FILE` (style, names, budget, claims with evidence) and
   `awb write check deliverables/FILE`.
4. For tier 3, read the text through each lens (the source platform, the target platform, the partner who sells
   and supports it, and a fidelity pass for invented facts, contradictions and a misread question). Apply the
   findings by rewriting the answer from the original question, never by patching it.
5. Record the result: `awb review pass deliverables/FILE`. `awb review status` shows every deliverable's state.
