# R-005 Arithmetic by a program, never in a reply

A number that a text computes (a total, a sum, a saving, a difference, an average, a product such as 20 x 8, a
monthly figure from an hourly price) is computed by `awb calc` and recorded in the project, never worked out in a
reply. The recorded id goes into the review as the evidence of the sentence that states the result.

Reason: a language model adds and multiplies by guessing the next digits. It is right often enough to be trusted
and wrong often enough to put a wrong total into an offer. Enforced by: `awb calc` (exact decimals, rounding half
up, a record per calculation in `calc/calc.tsv`), `awb review l0` (a high-risk sentence with a computed number needs
`calc:K-N` as evidence and the recorded result must appear in it), `awb price check` for price sheets.
