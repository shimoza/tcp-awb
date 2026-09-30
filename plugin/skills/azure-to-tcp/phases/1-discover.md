# Phase 1: discover

Orchestrator first: `awb migrate inventory input/FILE` (add `--table N` when the copy holds several tables). It
writes `migration/inventory.json` and names the rows that need a word.

Worker (files only): read `migration/inventory.json` and the sanitised copy in `input/`. Write
`migration/discover.md`:

1. What the inventory holds: the number of machines, the Azure families, the operating systems, what is missing.
2. Every row that needs a word, by its row id (R-1, R-2 ...): no vCPU or memory, a size name outside the naming
   convention, a D or DS size before v3, a constrained size, an Arm size. Say what is missing or what it means.
3. The questions for the customer, one line each, with the row ids they concern.

Use the row ids, never a machine name. Do not map, do not price. The gate checks that every such row id appears.
