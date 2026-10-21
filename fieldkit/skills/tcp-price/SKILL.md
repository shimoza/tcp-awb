---
name: tcp-price
description: Look up a T Cloud Public (TCP) price live from the public price API with the bundled script, stating region, flavor, price, unit and the date of the lookup
allowed-tools: Read, Bash(python3:*)
---

# Prices of TCP

A price is never stated from memory, from the facts or from any file. It comes from a live lookup in the public TCP
price API (no credentials needed), made with the script `price_lookup.py` in this skill's folder
(`${CLAUDE_SKILL_DIR}/price_lookup.py`). It uses Python 3 and nothing else.

## Look it up

1. Get the service (`ecs`, `evs`, `rds` ...), the region (default `eu-de`; others are `eu-nl`, `eu-ch2`) and, for
   compute, the flavor. Ask when the question does not say. Do not assume a region and do not carry a price of one
   region over to another.
2. Run:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/price_lookup.py" ecs --region eu-de --flavor s3.large.2
   ```

   `--grep WORD` narrows by product name or operating system, `--limit N` caps the rows.
3. Answer from the printed rows only. Give region, flavor (or product), the price with its term, the unit and the date
   of the lookup the script printed: "s3.large.2 in eu-de, pay as you go, 0.0418 EUR per hour, looked up 2026-10-21
   09:12 UTC". The numbers in this example are made up for the layout.

## Rules

- The script prints `not found` and exits 1 when the API has no row. Say "not found", say what you looked for, and
  stop. Do not give a price you remember or one "about like" another row.
- The script exits 2 when the API did not answer. Say so and give no price. Offer to try again later.
- Several rows match (operating systems, sizes): list them and ask which one is meant before you pick.
- Do the arithmetic only from the printed row, show it step by step (hours per month times the price, times the number
  of units) and say which figure you took. A month of 730 hours is an assumption: say so when you use it.
- A price is the price of the day of the lookup. A row is no proof that the item can still be ordered; `services.md` of
  the TCP Facts release says what is offered. A price that stands in the prose of a fact is not a price source.
