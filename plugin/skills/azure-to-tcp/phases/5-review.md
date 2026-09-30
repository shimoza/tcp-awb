# Phase 5: review

Orchestrator: review `deliverables/migration-plan.md` like every text that leaves the project.

1. `awb review init deliverables/migration-plan.md --request REQUEST.md --tier 3 --budget WORDS`.
2. `awb review claims deliverables/migration-plan.md`, then fill the evidence: `calc:K-N` for every total,
   `kb:KB-XXXX` or a file for every TCP fact, the artifacts of the migration for flavors and row ids.
3. The lenses of the review skill (source platform Azure, target platform TCP, the partner, fidelity). Apply the
   findings by rewriting the plan from the request, then `awb review pass`.

The gate passes when the review record of the plan is valid.
