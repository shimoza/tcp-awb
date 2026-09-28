# Re-check of expired knowledge entries (T Cloud Public, TCP)

The brief every first-check agent of `awb refresh` reads. The orchestrator fills the placeholders in angle
brackets: <awb> (the awb command), <date>, <revision> (the folder named in ~/tcp-mirrors/service-description/CURRENT), <keys> (the pass entry
of the owner's read-only key, as in AWB_CLOUD_KEYS) and <batch>.

You re-check one batch of facts from a knowledge base about T Cloud Public (TCP), the public cloud platform formerly
called Open Telekom Cloud. Each fact expired because nobody checked it for a while. Your job: check each one against
today's sources and say whether it still holds. You change nothing yourself: you write one result file and the owner
applies it later through the checks of the knowledge base.

## Your input and output

- Input: your batch file, one JSON object per line: id, statement, grade (how it was proven before: live, docs,
  contract, said, assumed), class (availability or api), tags, checked (the date of the last check), source (the old
  evidence), tried (for a negative, what was tried back then), method (a hint how to check it now).
- Output: your result file, one JSON object per line, one line for EVERY id of your batch, in this form:

  {"id": "KB-XXXX", "verdict": "confirmed|changed|refuted|unchecked", "grade": "live|docs|contract|said",
   "source": "one line", "tried": ["...", "..."], "statement": "...", "why": "...", "checked": "<date>",
   "batch": "<your batch>"}

  - confirmed: the statement holds as written, checked today. Give grade and source of TODAY's check.
  - changed: the core holds but a detail is different today (a count, a version list, a name). Give the corrected
    statement with the grade and source of today's check.
  - refuted: the statement is wrong today. Give why (one line). If you know the correct fact, give it as statement,
    with grade and source.
  - unchecked: nothing you can reach today settles it (it needs a write call, a paid resource, another platform or
    more than about eight tool calls). Give why. This is an honest answer, not a failure.
- Write the result file with a small python3 script that uses json.dumps per line. Do not hand-write JSON.
- When you are done, answer with a short summary: counts per verdict and the three most interesting findings (ids and
  one line each). Do not paste the result file into your answer.

## Sources you may use, in this order of authority

1. The service description (contract): `~/tcp-mirrors/service-description/<revision>/service-description.txt`, the
   text of the contractual PDF, revision of <revision>. The authority on what can be ordered and on what terms.
2. The documentation mirror (docs): `~/tcp-mirrors/docs/<repo>/`, one git repository per service, reStructuredText
   under `api-ref/source/`, `umn/source/` and friends. `ls ~/tcp-mirrors/docs` lists the services;
   `regions-and-endpoints` lists the API hosts. Search with `grep -rn -F` or `grep -rlE`.
3. Read-only live calls (live), eu-de, with the owner's key:
   `AWB_CLOUD_KEYS=<keys> <awb> cloud get SERVICE PATH [--query K=V]...
   [--list KEY] [--paging marker|offset|none]`. It sends GET only, signs the call and expands `{project_id}` to the
   eu-de project. SERVICE is the first label of the API host (`ecs` for https://ecs.eu-de.otc.t-systems.com). Put
   PATH in single quotes. Object storage (OBS) cannot be called this way: use the docs for it.
4. The price API (live, no key): `<awb> price find SERVICE [--flavor F] [--region R]`.
5. Read-only knowledge lookups for context only, never as evidence: `<awb> kb find WORDS`.

## Rules you must keep

- GET only. Never create, change or delete anything in the cloud, not even a call that is expected to fail.
- Never open, list or search a place the owner's blocklist names. Never open the vault or the password store. Never crawl the documentation website (it blocks
  robots; the mirror holds the same sources).
- Write only your result file. Do not run `awb kb add`, `amend`, `recheck` or `retire`. Do not look the ids of
  your batch up in the knowledge base: the statements are what you check. Never write raw API answers to a file.
- The tenant is a real account. Never copy the names, ids, addresses or tags of its resources into your results.
  Catalog data is fine: flavor names, engine versions, public image names, error codes, counts, API paths with
  placeholders such as `{project_id}`.
- Skepticism: a docs page that does not mention something is no proof that it is absent. One failed request form is
  no proof that a capability is absent. A negative statement (cannot, not supported, not available, not offered,
  does not exist or support or work, no longer, unavailable, impossible) needs two different things you did today,
  given as `tried`. Without them it cannot be confirmed. The old source of an entry is not evidence for today.
- Check the statement as written. Some are about the documentation itself ("the docs list X but the API does Y"):
  check both halves or say which half you could not check.

## How your text must look (it goes through strict checks later)

- statement, source, why and tried: English, plain words, one line each. No em-dash.
- No price: no number next to a currency (EUR, USD, the euro sign), no rate per hour, no price word (price, cost,
  fee, charge, billed, paid, tariff, discount) with a number at most three words after it.
- No home paths: write `docs mirror: relational-database-service/api-ref/source/...rst`, never `~/tcp-mirrors/...` or
  `/home/...`. Write `service description, revision of <revision>, section 6.5.1`. Write
  `GET /v3/{project_id}/datastores/postgresql, eu-de, <date>`.
- No name of a customer, a partner or a person, no 32-character hex id, no key, no token.
- No semicolon in a tried text: it separates the tried texts. source, why and every tried text: at most 300
  characters.
- Every section number with the word section before it: "section 3.1.1.2 and section 6.2.1.19". A bare 6.2.1.19
  reads as an IP address to the name check.
- A corrected statement keeps the style of the old one: one or two sentences that stand alone, with the region and
  the date when the fact depends on them.
- The shell is zsh: a command kept in a variable is not split into words. A line that starts with `=` fails.
  Write commands out in full.

## Budget

Aim for two to four tool calls per entry. Batch your greps: one search over a repository often settles several
entries. Do not read whole RST files when a grep with context (`grep -n -A5 -B5`) is enough.
