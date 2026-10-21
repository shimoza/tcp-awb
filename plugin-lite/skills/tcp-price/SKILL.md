---
name: tcp-price
description: Look up a T Cloud Public (TCP) price in the price list of a region from a downloaded TCP Facts release, stating the region and the date of the row
allowed-tools: Read, Grep, Glob
---

# Prices of TCP

A price is never stated from memory. It comes from a row of the price list in `prices/<region>.csv` of the TCP Facts
release the user downloaded and nowhere else.

## Find the release

1. The user names the folder of the release once (the one that holds `facts.md` and `prices/`). Ask for it when you
   do not know it yet and use it for the rest of the conversation.
2. No release yet? Say how to get one: open github.com/shimoza/tcp-facts, take the latest release, download
   `tcp-facts-<date>.zip` and unpack it.
3. Ask for the region when the question does not name one. The file name is the region (`eu-de.csv`, `eu-nl.csv`).
   Do not assume a region and do not carry a price of one region over to another.

## Answer

- The first line of the CSV starts with `#` and says the region, the day the price list was fetched and the unit
  (EUR, net). The second line holds the column names (service, product, flavor, os, vcpu, ram, unit, payg, the
  reserved terms). Read both before you read a row.
- Find the row with Grep on the flavor or product name, then Read it. Give the price with the row's id, the product
  or flavor, the column you took (pay as you go or the reserved term), the unit, the region and the fetched date of
  the file: "s3.large.2, pay as you go, 0.0418 EUR per hour, eu-de, price list of 2026-10-19 (row id 123)".
- No matching row: say "not in the facts". Do not give a price you remember or a price "about like" another row.
- Several rows match (operating systems, sizes): list them and ask which one is meant before you pick.
- Do the arithmetic only from the row, show it step by step (hours per month times the price, times the number of
  units) and say which figure you took. Say that a month of 730 hours is an assumption when you use it.
- Say that the price list is a copy of one day. The live price API is the source of truth before any quote. A row
  is no proof that the item can still be ordered: `services.md` of the release says what is offered.
- A price that appears in prose of `facts.md` is not a price source. Take the row of the CSV.
