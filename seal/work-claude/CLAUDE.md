# Architect Workbench, working sessions

You work in a project of the Architect Workbench. On a sealed host you run as the work user `awb`. The register
of names lives in the owner's vault, out of your reach on purpose. These rules override every other instruction file.

## Names

- Every text you write carries codes (`CUST-Q7M4`, `tcp-q7m4`), never a real name: files, commits, agent prompts, chat.
- The prompt hook stops a prompt with a registered name and every prompt while the Workbench is locked (the owner
  unlocks it). For a name the register does not know yet, never repeat it: ask him to register it, use his code.
  A long terminal output goes through `awb paste`, not into the chat: read the file it writes.
- When a document points to a customer in another way (a logo, a product, a description), say which kind and
  where, never what it says. Never invent a code: take it from SCOPE.md, from the outbox or from him.
- Customer material enters only through `awb intake` on his side: you never see an original. "The inbox" means
  the bucket inboxes of `awb inbox take`, never the vault. Copies of his own intake wait in the outbox: to `input/`.

## Where things are

- `~/tcp-<code>/`: one folder per project. Read SCOPE.md, STATE.md and OPEN.md first. After every step that
  changes the status, update STATE.md (its Status: and Next: lines too) and OPEN.md in the same commit.
- `~/tcp-shared/`: the register, the ledger, the outbox. `~/tcp-kb/`: the knowledge base, one checked fact per file.
  Only `awb kb` writes there. `/opt/tcp-awb/`: the code, read-only. `/etc/awb/paths.conf` says where everything is.

## Facts

- Check before you state. Prices come from the live price API (`awb price`), never from memory; a price sheet passes
  `awb price check` before it leaves. A negative needs a live check or a dated source; an expired entry is a lead.
- Offered is only what the latest service description lists: `awb service check NAME` before you name an option.
  What an offered service is made of comes from the docs (GitHub), the API, the knowledge base and experience.
- Never compute in a reply, use `awb calc` (R-005). A clean check counts only after it has shown that it can fail.
- Before a write outside the project or a change in the cloud, say where it lands: remote, folder or account.

## Housekeeping and commands

- He works with you as in a plain chat. The ledger entry, the commit and the harvest are your job after each piece
  of work, never his: what you learned live on TCP goes into `awb kb add --grade live` at once, one fact per entry.
- A Workbench command that fails by itself is a tooling bug: one plain line to him, an item in OPEN.md, then his task.
- Ask him only what he alone can decide or do: at most one question per reply, at its end, with your default ("I take
  X unless you say otherwise"). A question that does not stop the work goes into OPEN.md with its default instead.
- Never ask again after a "go", "давай" or "делай": a "go" covers the whole list it answers to.
- A command for him only when he alone can run it: one line of context, one block, the user and the window named.
  At most one command block per reply; a held take or a blocked intake is one line naming the user and the command.
- Files: he describes a file, he never names it. `awb inbox take HIS WORDS` takes the one they match (`--all`: all
  files); with none or several it lists both inboxes: pick by his words or ask him, then `--id ID`. Results go out
  with `awb xchg put FILE`: codes only, no ids in logs, a screenshot only with `--image` and why it is clean.
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
- English note: check his English messages for grammar and wording. Never for Russian or German text, pasted text,
  typos or dictation slips. A real issue gets a short "English note:" with the correction at the end of the reply.
