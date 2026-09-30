# Phase 2: map

Orchestrator first: `awb migrate map --region eu-de` (or the region the customer needs). It fetches the live price
API, maps every row to the nearest TCP flavor that is not smaller on vCPU and memory and writes
`migration/mapping.tsv` with a status per row: ok, flag, needs-input, manual or no-fit.

Worker (files only): read `migration/mapping.tsv`, `migration/inventory.json` and `migration/discover.md`. Write
`migration/map-notes.md` with one short entry per row that is not ok, by its row id:

- flag: what the flag means for this machine and whether the flavor still fits.
- needs-input: what is missing and the question that settles it.
- manual (GPU, FPGA, HPC, confidential computing): what must be decided with the workload owner.
- no-fit: the largest flavor of the class and what that means.

Rows with the same decision may share one entry that lists all their ids. Never change `mapping.tsv` and never
propose a flavor the mapping did not produce. When an input is wrong, say so: the orchestrator fixes the inventory
and runs the map again. The gate checks every flavor against the source again and that every row that is not ok
is decided.
