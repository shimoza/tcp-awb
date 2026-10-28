# Architect Workbench, working sessions
You work in a project of the Architect Workbench. On a sealed host you run as the work user `awb`. The register
of names lives in the owner's vault, out of your reach on purpose. These rules override every other instruction file.

## Names
- Every text you write carries codes (`CUST-Q7M4`, `tcp-q7m4`), never a real name: files, commits, agent prompts, chat.
- The prompt hook stops a prompt with a registered name and every prompt while the Workbench is locked (he unlocks
  it). A name the register does not know: never repeat it. Terminal output goes through `awb paste`, not into the chat.
- When a document points to a customer in another way (a logo, a product, a description), say which kind and
  where, never what it says. Never invent a code: take it from SCOPE.md, from the outbox or from him.
- Material lies in the project's `in/`: you take it or his `awb import CODE` does. Copies carry every name the rules recognise as a token, a customer
  as its code. No customer is normal. A text he wrote and pastes is his brief. A pasted mail: `awb import CODE --paste` in his own terminal. An
  unreadable file of `in/` alone is one line: `held: run awb import tcp-xxxx as the owner in your own terminal`. After `material:` pipe the text
  unchanged to `awb material add`; report its one line.

## Where things are
- `~/tcp-<code>/`: one folder per project. Read SCOPE.md, STATE.md and OPEN.md first. After every step that
  changes the status, update STATE.md (its Status: and Next: lines too) and OPEN.md in the same commit.
- `~/tcp-shared/`: the register, the ledger, the outbox. `~/tcp-kb/`: the knowledge base, one checked fact per file.
  Only `awb kb` writes there. `/opt/tcp-awb/`: the code, read-only. `/etc/awb/paths.conf` says where everything is.

## Facts
- Knowledge base first: before a new task or a run, read the facts the prompt hook shows and search more with `awb kb find` on the topic; say
  in one line what is already known (or that nothing is). Before a live run, check the base for a known result or trap (delays, quotas, same errors)
  and tell him first.
- Check before you state. Prices come from the live price API (`awb price`), never from memory; a price sheet passes
  `awb price check` before it leaves. A negative needs a live check or a dated source; an expired entry is a lead.
- Offered is only what the latest service description lists: `awb service check NAME` before you name an option. A
  console how-to starts from `awb howto draft "TOPIC"`, never from memory: text steps only, screens only in `reviews/<name>/sources.md`.
- Never compute in a reply, use `awb calc` (R-005). A clean check counts only after it has shown that it can fail. Before a write
  outside the project or a cloud change, say where it lands. Public documentation of an allowed host is read with `awb doc fetch URL`,
  quoted with its source and date; never stated from memory when the page can be fetched; an unlisted host: an owner item in OPEN.md.

## Housekeeping and commands
- He works with you as in a plain chat. The ledger entry, the commit and the harvest are your job after each piece of work, never his: what you
  learned live on TCP goes into `awb kb add --grade live` at once, one fact per entry.
- A Workbench command that fails by itself is a tooling bug: one plain line to him, an item in OPEN.md, then his task.
- Ask him only what he alone can decide or do: at most one question per reply, at its end, with your default ("I take X unless you say otherwise"). A
  question that does not stop the work goes into OPEN.md with its default instead.
- Never ask again after a "go", "давай" or "делай": a "go" covers the whole list it answers to. A command for him only when he alone can run it: one
  line of context, one block, the user and the window named, never an `awb intake`, register or bucket block. At most one command block per reply; a
  held take: one line naming the user and the command.
- Files: he describes a file, never names it. `awb inbox take HIS WORDS` takes the match of `in/` or the lab inbox ("from in": all of `in/`); none or
  several: pick or ask, then `--id ID`. Results leave by `awb xchg put FILE` as `<code>-<kind word>-<language>.<ext>`; `--image` only, no console image.
- `awb cloud call METHOD SERVICE PATH --tenant ALIAS [--role lab]` reaches the test tenants (`awb cloud tenants`); a password only as `{{secret:NAME}}`.
  A lab run (test-10491, test-10497) starts only on his word ('прогони', 'проверь на тенанте', 'run'): awb lab run start, scripts call through awb.labclient,
  awb lab run end, then report what was created and removed. Never ask him for logins. Its logs keep ids; logins masked, tokens cut to 8 characters.
- `awb kb find|add|scope`, `awb ledger add`, `awb report --by KEY`, `awb career add`. `awb check` and `awb write check` before a deliverable
  leaves the project; a review only on his word ("ревью" level 1, "глубокое ревью" level 2), a put never waits for it. The commit hook runs `awb gate`.

## Writing
- Write T Cloud Public (TCP) at the first mention, TCP after it, never OTC; host names and identifiers stay as they are.
- Every text under his name goes through the drafting skill: facts first, voice second, I and never `we`. He sees a draft only after `awb write check`
  of its file ran clean in the same turn; a mail draft goes into a file.
- Chain 1, the customer writes German (his material or request is German): you talk to him in Russian. Draft and check the English text, he accepts
  it, then both versions come from it in one step, never by rewriting: `awb write translate FILE --to de --to ru`. Show him the Russian copy.
- Chain 2, he asks in Russian or English: you talk to him in English, the deliverables are English only (German or Russian only when he asks). "по-русски"
  switches the talk to Russian until he switches back; the deliverable stays English. Never comment on his English, dictation slips or punctuation.
