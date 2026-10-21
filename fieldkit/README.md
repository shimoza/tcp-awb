# tcp-fieldkit

An independent, community-made toolkit for T Cloud Public (TCP). Not a product of the operator; it speaks for T-Systems or Deutsche Telekom in no way.

It is a light Claude Code plugin for partners with two skills. `tcp-facts` answers from the files of a TCP Facts
release you downloaded. `tcp-price` looks a price up live in the public TCP price API. It has no hook and no command
of the Architect Workbench, and it needs no credentials.

| skill | what it does |
|---|---|
| `tcp-facts` | answers from `facts.md`: every fact with its id and checked date, "not in the facts" when nothing matches |
| `tcp-price` | looks a price up live with a small Python script (standard library only) and states region, flavor, price, unit and the date of the lookup; "not found" when the API has no row |

## Install

```bash
claude plugin marketplace add shimoza/tcp-awb
claude plugin install tcp-fieldkit@tcp-awb
```

## Get the facts

1. Open github.com/shimoza/tcp-facts and take the latest release.
2. Download `tcp-facts-<date>.zip` and unpack it. The folder holds `facts.md`, `facts.jsonl`, `services.md` and
   `topics/`.
3. In Claude Code, name the folder once: "My TCP Facts are in ~/Downloads/tcp-facts-2026-10-19". The skill asks for it
   when it needs it.

A release is good for 30 days (the best-before date is at the top of `facts.md`). Download a newer one when it has
passed. Prices are not part of a release; the price skill never reads a file.

## Use

- "Which flavors does ECS offer in eu-de?" The answer cites fact ids and their check dates.
- "What does an s3.large.2 cost per hour in eu-nl?" The skill runs the lookup and the answer names the region, the
  flavor, the price, the unit and the date of the lookup.
- A question the facts do not cover gets "not in the facts". A price the API does not list gets "not found".

## The rules the skills follow

- Facts: answer only from the files of the release, cite the fact id and its checked date, say "not in the facts"
  when nothing matches.
- Prices: never from memory. Every price comes from a live lookup, with its region and the date of the lookup.

## What it needs

`tcp-price` runs `python3` and reaches the public price API over HTTPS. `tcp-facts` reads files only.

## What it is not

It is not the Architect Workbench. It does not check, import or change anything. For a knowledge base on your laptop
(`awb kb import facts.jsonl`) install the `awb` plugin of the same marketplace. The facts are licensed CC BY 4.0, the
plugin Apache 2.0.
