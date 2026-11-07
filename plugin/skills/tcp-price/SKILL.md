---
name: tcp-price
description: Look up or check a T Cloud Public (TCP) price or check a price sheet to the cent, from the live price API
---

# Prices of TCP

A price is never taken from memory or from the knowledge base. It comes from the live public price API.

- One service: `awb price find SERVICE [--flavor F] [--os TEXT] [--grep TEXT]`, for example
  `awb price find ecs --grep s3.large`. Add `--region eu-nl` or `--region eu-ch2` for another region.
- A price sheet before it leaves: `awb price check SHEET`. It fetches every price again and recomputes every line
  and the totals to the cent. Report what it finds, line by line.
- A whole estimate as a workbook: write the specification (`awb pricebook build --help` and COMMANDS.md show the
  format), then `awb pricebook build SPEC --lang de`. It prices every line live, refuses a service that is not
  offered and writes the workbook and a summary for a mail. `--against OLD` adds what changed against an earlier
  estimate; `awb pricebook diff OLD NEW` shows it as text.
- Never add, multiply or take a percentage in your head. Every sum, a monthly figure from an hourly price and a
  saving goes through `awb calc "730 * 0.0418 * 3" --places 2` in the project. It records the calculation as
  K-N; give `calc:K-N` as the evidence of the sentence in the review.
- Give every price with its unit, its term (pay per use or a reserved term) and the time it was fetched, as the
  command prints it.
- A price record does not prove that the item can still be ordered. Say so when availability matters.

## Reserved for many machines

Reserved is a monthly contingent per billing item (flavor plus OS), shared by every instance of it. It is never
counted per instance.

- Count it with `awb price pool SERVICE --flavor F --os OS --hours H --term R24`, where H is the pooled hours of all
  instances of the item in a month. It chooses the units once over the term and prints the figures with the time
  they were fetched. Add `--start YYYY-MM-DD` for the real months. In a workbook write `mode: pool` with
  `pooled_hours:` and `term:`.
- Windows and Linux of one flavor are two items: price each on its own, never pooled together.
- Name the open points every time: the minimum order volume is agreed with Telekom Sales, the lead time is five
  working days, unused hours expire and use beyond the units runs Open Elastic. For a GPU flavor say that whether a
  stopped GPU flavor is billed is open (KB-NAAF). A term with the rate 0 is not offered.
- Never from memory: no unit count, rate or saving that `awb price pool` did not print.
