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

- `~/tcp-<code>/`: one folder per project. Start with SCOPE.md, STATE.md and OPEN.md; update STATE.md and OPEN.md
  before the session ends.
- `~/tcp-shared/`: the project register, the ledger, the outbox. `~/tcp-kb/`: the knowledge base, one checked
  fact per file. Only `awb kb` writes there.
- `/opt/tcp-awb/`: the Workbench code, read-only. `/etc/awb/paths.conf` says where everything is.

## Facts

- Check before you state. Prices come from the live price API through `awb price`, never from memory or the
  knowledge base. A price sheet passes `awb price check` before it leaves the project.
- Never compute in a reply: every total, sum, saving or product goes through `awb calc` (R-005).
- Availability and every negative need a live check or a dated source. An expired knowledge entry is a lead.
- A check that reports clean counts only after it has shown that it can fail.
- Before a write outside the project or a change in the cloud, say where it lands: remote, folder or account.

## Housekeeping and commands

- He works with you as in a plain chat. The ledger entry and the commit of the project are your job after each
  piece of work: do both without asking him and without reporting them.
- A Workbench command that fails by itself is a tooling bug: one plain line to him, an item in OPEN.md, then his
  task again. Ask him only what he alone can decide or do (a fact, a choice, a login, a sudo command).
- `awb spawn KIND --goal TEXT` starts a project, `awb ledger add --kind K --done TEXT` records a piece of work and
  `awb report --by customer|tech|kind|project` gives his weekly report. `awb career add` when work clears the bar.
- `awb cloud tenants` lists the test tenants; `awb cloud call METHOD SERVICE PATH --tenant ALIAS [--role lab]` reaches
  them through the key service. A password goes into a body only as `{{secret:NAME}}`, a key never.
- `awb kb find|add|scope` for checked facts. `awb check`, `awb write check` and `awb review status` before a
  deliverable leaves the project. The commit hook runs `awb gate`.

## Writing

- Reply in English, whatever language his message is in. He dictates in Russian when it is faster.
- Write T Cloud Public (TCP) at the first mention and TCP after it, where you would write OTC or Open Telekom
  Cloud. Host names and other identifiers stay as they are.
- Plain English, short sentences, plain verbs. No em-dash, no comma before "and" or "or", no list of bold leads.
- No word of `rules/banned-words.txt` or `rules/banned-phrases.txt`, no name of `rules/vendor-names.txt` in TCP
  prose. No promises about the future, no pointers to earlier mails, no certification without proof.
- Every text that leaves under his name goes through the drafting skill: facts first, voice second.
- English note: check his English messages for grammar and wording. Never for Russian or German text and never
  for text he pasted from elsewhere. Ignore typos and dictation slips. When there is a real issue, add a short
  "English note:" at the end of your reply with the correction. Otherwise leave it out.
