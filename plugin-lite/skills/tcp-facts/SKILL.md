---
name: tcp-facts
description: Answer a question about T Cloud Public (TCP) services, limits, flavors or regions from the facts.md of a downloaded TCP Facts release, citing fact ids and check dates
allowed-tools: Read, Grep, Glob
---

# Facts about TCP

Answer only from the files of a TCP Facts release. Nothing else counts as a source: not your memory, not a web
search, not another file.

## Find the release

1. The user names the folder of the release once (the one that holds `facts.md`). Ask for it when you do not
   know it yet and use it for the rest of the conversation.
2. No release yet? Say how to get one: open github.com/shimoza/tcp-facts, take the latest release, download
   `tcp-facts-<date>.zip` and unpack it. The folder with `facts.md` in it is the one to name.
3. Read the top of `facts.md` first. It carries the rules of the release and the best-before date. When that date
   has passed, tell the user and suggest a newer release before you answer.

## Answer

- Search `facts.md` with Grep for the key words of the question (service, flavor, region, limit), then Read the
  matching lines. `topics/<tag>.md` holds the same facts grouped by topic; `facts.jsonl` holds them with source,
  class and expiry when the user wants the source of one.
- One line of `facts.md` is one fact. Give each fact you use with its id and its checked date, like this: "The
  quota is fifty per project (KB-XXXX, checked 2026-10-01)."
- A fact older than 90 days or one the file marks as expired, is said so in the answer.
- When no line matches, say "not in the facts". Do not guess, do not fill the gap with what you know about cloud
  platforms, do not say "probably". You may say which words you searched for.
- When two facts disagree, give both with their dates and let the newer one lead.
- `services.md` lists the services that can be ordered. A service that is not in it is not offered, whatever
  other texts say.
- A question about a price is not answered from a fact. Use the `tcp-price` skill.
- Quote the fact as it stands. Do not make it stronger, do not merge two facts into a third.
