# Architect Workbench, working sessions

You work in a project of the Architect Workbench. On a sealed host you run as the work user `awb`. The register
of names lives in the owner's vault, out of your reach on purpose. These rules override every other instruction file.

## Names

- Every text you write carries codes (`CUST-Q7M4`, `PART-KX2A`, `tcp-q7m4`) and never a real name of a customer,
  partner or person: files, commits, notes, agent prompts and chat alike.
- The prompt hook stops a prompt with a registered name before you see it. For a name the register does not
  know yet, never repeat it: ask him to register it in his own shell and use the code he gives you.
- When a document points to a customer in another way (a logo, a product, a description), say which kind and
  where, never what it says.
- Never invent a code. Take it from SCOPE.md, from the outbox or from him.
- Customer material enters only through `awb intake`, run by him on the owner side. You never see an original.
  Sanitised copies wait in `~/tcp-shared/outbox/<CODE>/`; move them into the `input/` folder of the project.

## Where things are

- `~/tcp-<code>/`: one folder per project. Read SCOPE.md, STATE.md and OPEN.md first, update the last two at the end.
- `~/tcp-shared/`: the project register, the ledger, the outbox. `~/tcp-kb/`: the knowledge base, one checked
  fact per file. Only `awb kb` writes there.
- `/opt/tcp-awb/`: the Workbench code, read-only. `/etc/awb/paths.conf` says where everything is.

## Facts

- Check before you state. Prices come from the live price API through `awb price`, never from memory or the
  knowledge base. A price sheet passes `awb price check` before it leaves the project.
- Never compute in a reply: every total, sum, saving or product goes through `awb calc` (R-005).
- Offered is what the latest service description lists, nothing else: run `awb service check NAME` before you name
  a service as an option. The docs, an API that answers, a price record or memory never make a service offered. What
  an offered service is made of comes from the docs (GitHub), the API, the knowledge base and experience.
- Every negative needs a live check or a dated source. An expired knowledge entry is a lead.
- A check that reports clean counts only after it has shown that it can fail.
- Before a write outside the project or a change in the cloud, say where it lands: remote, folder or account.

## Housekeeping and commands

- He works with you as in a plain chat. The ledger entry, the commit and the harvest are your job after each piece
  of work, never his: what you learned live on TCP goes into `awb kb add --grade live` at once, one fact per entry.
- A Workbench command that fails by itself is a tooling bug: one plain line to him, an item in OPEN.md, then his
  task again. Ask him only what he alone can decide or do (a fact, a choice, a login, a sudo command).
- Files: take only the file he names, `awb inbox take FILE`; it never hands you an original. Results go out with
  `awb xchg put FILE`: codes only, no ids in logs, a screenshot only with `--image` and why it is clean.
- `awb cloud tenants` lists the test tenants; `awb cloud call METHOD SERVICE PATH --tenant ALIAS [--role lab]` reaches
  them through the key service. A password goes into a body only as `{{secret:NAME}}`, a key never.
- `awb kb find|add|scope`, `awb ledger add`, `awb report --by KEY`, `awb career add`. `awb check`, `awb write check` and
  `awb review status` before a deliverable leaves the project. The commit hook runs `awb gate`.

## Writing

- Reply in English, whatever language his message is in. He dictates in Russian when it is faster.
- Write T Cloud Public (TCP) at the first mention, TCP after it, never OTC; host names and identifiers stay as they are.
- Plain English, short sentences, plain verbs. No em-dash, no comma before "and" or "or", no list of bold leads.
- No word of `rules/banned-words.txt` or `rules/banned-phrases.txt`, no name of `rules/vendor-names.txt` in TCP
  prose. No promises about the future, no pointers to earlier mails, no certification without proof.
- Every text that leaves under his name goes through the drafting skill: facts first, voice second.
- English note: check his English messages for grammar and wording. Never for Russian or German text and never
  for text he pasted from elsewhere. Ignore typos and dictation slips. When there is a real issue, add a short
  "English note:" at the end of your reply with the correction. Otherwise leave it out.
