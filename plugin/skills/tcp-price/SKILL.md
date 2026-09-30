---
name: tcp-price
description: Look up or check a T Cloud Public (TCP) price, or check a price sheet to the cent, from the live price API
---

# Prices of TCP

A price is never taken from memory or from the knowledge base. It comes from the live public price API.

- One service: `awb price find SERVICE [--flavor F] [--os TEXT] [--grep TEXT]`, for example
  `awb price find ecs --grep s3.large`. Add `--region eu-nl` or `--region eu-ch2` for another region.
- A price sheet before it leaves: `awb price check SHEET`. It fetches every price again and recomputes every line
  and the totals to the cent. Report what it finds, line by line.
- Never add, multiply or take a percentage in your head. Every sum, a monthly figure from an hourly price and a
  saving goes through `awb calc "730 * 0.0418 * 3" --places 2` in the project. It records the calculation as
  K-N; give `calc:K-N` as the evidence of the sentence in the review.
- Give every price with its unit, its term (pay per use or a reserved term) and the time it was fetched, as the
  command prints it.
- A price record does not prove that the item can still be ordered. Say so when availability matters.
