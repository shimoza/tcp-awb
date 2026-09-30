# Phase 3: estimate

Worker (shell): in the project folder run `awb migrate estimate --term PAYG`. Run it again with `--term R12` or
`R24` when the customer asked about reserved terms. Each run prints the total per month and the calculation id
(calc:K-N) that holds it.

Then write `migration/estimate-notes.md`:

1. Per term the total per month with its calculation id, as `awb migrate estimate` printed it.
2. The rows left out of the estimate and why (needs-input, manual, no price for the OS and term).
3. What the estimate does not hold: disks, network, backup, licences beyond the OS tier of the price record.

Never compute a number yourself. A subtotal or a difference between terms goes through `awb calc` and its id goes
next to the number. The gate checks that every calculation id you name is recorded.
