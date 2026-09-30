# Phase 4: plan

Worker (files only): read `migration/discover.md`, `migration/map-notes.md`, the `migration/estimate-*.tsv` files and
`migration/estimate-notes.md`. Write `deliverables/migration-plan.md` for the customer:

1. The answer first: how many machines move, to which TCP flavor families, what it costs per month (the totals
   with their `calc:K-N`, never a new number).
2. The target in short: region, flavor families, what stays open (the rows that need input or a decision).
3. The move in waves: which rows go first and why (least dependent, least risk), what is checked before each wave.
4. The risks and the questions for the customer.

Say that a price record does not prove that a flavor can be ordered, unless a test tenant has shown it. Write for
the customer in plain English. Row ids, flavors and numbers come from the artifacts only.
