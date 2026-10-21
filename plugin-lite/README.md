# tcp-facts-lite

A light Claude Code plugin for partners. It answers questions about T Cloud Public (TCP) from the files of a TCP
Facts release you downloaded and from nothing else. It has two skills and no command, no hook and no network access.

| skill | what it does |
|---|---|
| `tcp-facts` | answers from `facts.md`: every fact with its id and checked date, "not in the facts" when nothing matches |
| `tcp-price` | answers a price from the row of `prices/<region>.csv`, with the region and the date of the price list, never from memory |

## Install

```bash
claude plugin marketplace add shimoza/tcp-awb
claude plugin install tcp-facts-lite@tcp-awb
```

## Get the facts

1. Open github.com/shimoza/tcp-facts and take the latest release.
2. Download `tcp-facts-<date>.zip` and unpack it. The folder holds `facts.md`, `facts.jsonl`, `services.md`,
   `topics/` and `prices/` (one CSV per region).
3. In Claude Code, name the folder once: "My TCP Facts are in ~/Downloads/tcp-facts-2026-10-19". The skills ask for it
   when they need it.

A release is good for 30 days (the best-before date is at the top of `facts.md`). Download a newer one when it has
passed.

## Use

- "Which flavors does ECS offer in eu-de?" The answer cites fact ids and their check dates.
- "What does an s3.large.2 cost per hour in eu-nl?" The answer names the CSV row, the region and the date of the
  price list.
- A question the facts do not cover gets "not in the facts".

## The rules the skills follow

- Answer only from the files of the release.
- Cite the fact id and its checked date.
- Say "not in the facts" when nothing matches.
- Never state a price from memory. A price comes from the CSV row, with its date and region.

## What it is not

It is not the Architect Workbench. It does not check, import or change anything. For the knowledge base on your
laptop (`awb kb import facts.jsonl`) install the `awb` plugin of the same marketplace. The facts are licensed
CC BY 4.0, the plugin Apache 2.0.
