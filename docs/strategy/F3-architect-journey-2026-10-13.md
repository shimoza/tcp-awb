# F3: the architect's journey through the Workbench

Date 2026-10-13. An analysis, no code change. Codes in the examples are the invented ones of the tests: CUST-Q7M4 and
tcp-q7m4.

## Scope and method

The journey was walked as the owner lives it, from README.md, COMMANDS.md, INSTALL.md, the plugin skills, the project
templates in `awb/projects.py`, the work rules in `seal/work-claude/CLAUDE.md` and, where the docs were unclear, the
code of the hooks, the key service, the exchange and the review. No host, tenant or vault was used: what a command
prints was read from its code.

The owner is one architect with about five projects at once. He dictates in Russian by voice, wants English
deliverables in his own voice and does not want to remember commands. He has five places to be: his own terminal
(the owner side), a shell of the work user (projects and sessions), the OBS console (files in and out), the web
console (board, chat, Ask) and his mail client. The place switch is the friction that runs through every step below.

Four boundaries must hold: names only on the owner side; the passphrase and the keys only in his hands; a cloud write
only under his grant; nothing leaves without the review and the send gate. No proposal below moves any of them.
Effort is S (under a day), M (a few days) or L (a week or more).

## The journey

### 1. A mail or a tender arrives

What he must know: whether the customer has a code, the tag list of `rules/tags.txt` and the order of four commands
in two windows. A new customer gets its code and its written forms on the owner side in one of two ways: `awb register new CUST`
with one `awb register add` per form or `awb import tcp-q7m4 --customer new`, which asks for the forms. The project is spawned as the work user: `awb spawn project --goal "..." --customer CUST-Q7M4 --tag ecs`.
For a customer project the grant is a third command in his own terminal, `awb projects kind tcp-q7m4 project` (or
`query`). Without it the first take from the owner inbox is held and a tenant call is refused with the line to run.

Only he: the names (R-001) and the grant, which is a cloud write under his key.

Friction: two ways to register a customer. The order register, spawn, grant, import stands in three places of the
docs and in none as a list. The tag must be known before the spawn or the spawn is refused.

Proposal P1: one owner command, `awb start project|query --goal "..." [--customer CUST-Q7M4|new]`. It asks for the
forms of a new customer as the import does, spawns as the work user through `sudo -n` as `awb projects open` does,
writes the grant, imports what lies in `in/` and ends with the one line to open the session. Effort M. Boundary: the
names stay in his terminal, the spawn still runs as the work user and the grant is still his. Nothing is weakened.

### 2. The material goes in

Six doors today: the owner inbox in OBS (a session takes with `awb inbox take "the pdf"`), the project's `in/`
folder in OBS ("take the files from in"), `awb import tcp-q7m4 FILE` in his terminal, `awb import --paste --kind
mail|transcript|table` from the clipboard, `awb bucket pull` and `awb intake` as the pieces of the import. The lab
inbox is a seventh door for material without customer content. The choice of inbox is his at the drop.

Only he: the intake of an original, because the vault and the register are his. The paste door, because a pasted
mail carries a signature and the prompt hook refuses it in the session.

Where it is silent: the import prints counts and the session reads "new input" at its next prompt. He hears nothing
about what the session did with the copies.

Where the error is unclear: every refused take is the one line `held: run awb import tcp-q7m4 as the owner in your
own terminal`. That is by design, so no word of a hidden name leaks. But the same line answers a missing grant, a
file over `max_mb`, an unreadable file and a folder with the wrong code. For the grant the advice is wrong: the
import does not cure it. The mail of a hold carries one of three reasons, the terminal line none.

Proposal P2: keep the one line and add a class word that names no value: `held (grant)`, `held (size)`, `held
(unreadable)`, `held (folder)`. For the grant the line names `awb projects kind`. Effort S. Boundary: a class word
carries no name and the mail carries the same reason already.

Proposal P3: one list "the doors" in COMMANDS.md: which door for a file, a mail, a transcript, a table and a vendor
image, with the window for each and the pieces (`bucket pull`, `intake`) below it. No code change. Effort S.

### 3. The project is opened

What he must type: `awb projects open tcp-q7m4` in his terminal, which needs `sudo -n` for the work user. Else
`ssh` to the work user, `cd` into the project and `claude`. Five projects are five terminals and five codes by
heart. The start prints the guards, SCOPE, STATE and OPEN and warns of a second session in the same project.

Silent: nothing tells him which of the five waits on him. The board has "waits on" per project, but the board is a
page for management, not his morning line.

Proposal P4: `awb projects open` without a code lists the active projects with their `Next:` line and the owner
items of OPEN.md, numbered. He picks one and it opens. Effort S. Boundary: none, it reads what the board reads.

### 4. Questions are answered

In the session he asks in plain words, in Russian when that is faster. The reply comes in English. The session
looks facts up, prices, checks what is offered and computes with `awb calc`. He types no command. This step works.

Friction: when he dictates a customer's name, the prompt hook refuses the prompt and tells him to write the code. He
must look the code up: `awb register list` shows codes without forms and `--forms` works on the vault side only. A
Russian spoken form is found only when a Russian written form is registered. The register knows umlaut spellings and
genitives, not Cyrillic.

Three ways to ask: the session, the Ask page or project chat of the console (a daily budget, it cannot act) and
`awb ask QUESTION`. The docs say what each reaches, not when to use which.

Proposal P5: the forms prompt of the first import asks for the German, English and Russian written forms in its
text. `awb register add` takes several forms in one call. Effort S. Boundary: none, the forms are typed on the
owner side as today.

Proposal P6: an owner-side `awb register find "words"` that prints the code of a form in his terminal only, refused
in a pipe or a session as `awb vault show` is. Effort S. Boundary: the owner reads the forms today; this prints less.

### 5. A lab runs on a test tenant

What the session must know: the project is of the kind project and granted; the tenant alias from `awb cloud
tenants`; the recipe from `awb lab list`; the secrets a recipe fills exist in his password store; the run goes to the
background because a foreground command ends after two minutes; exit 4 means a resource is left and he deletes it.

Only he: the switch of a query to a project, because the grant is a cloud write under his key; the secrets in the
password store; a resource the teardown cannot delete.

Two ways: a recipe with `awb lab run` and a call by hand with `awb cloud call --role lab`. Three recipes exist.

Unclear: `awb lab run` wants `--project` although it runs from the project's folder and the key service takes the
code from the folder anyway. A session in a query that needs a live test says so and waits for the switch in his
terminal. Nothing tells him until he reads that reply.

Proposal P7: `--project` defaults to the folder's code and `--tenant` to a `tenant:` line of SCOPE.md that the spawn
writes when one tenant exists. Effort S. Boundary: none, the key service checks the grant as before.

Proposal P8: the switch request goes to his mail. When a session writes an owner item into OPEN.md that names a
command only he can run, the stop hook sends one mail through the SMN topic with the project code and the fixed
command word, no free text. Effort S. Boundary: the mail carries a code and a fixed word, as the exchange mails do.

Proposal P9: more recipes for the checks he runs most (the work profile counts 391 live checks). A recipe is one
JSON file and no code. Effort M per five recipes.

### 6. A deliverable is drafted, reviewed and checked

The chain in the session: `awb review init` with a request file, a tier and a budget in words; `claims`; the
evidence per claim; `l0`; for tier 2 and 3 the saved workflow `review.js`, then `request-check.js`; `pass`; the
before-voice copy; the voice pass; `awb write keep`; `awb write check`; the cut; both checks again; then the draft
is shown. The German version and his Russian copy come from `awb write translate`, which in a work session writes the
prompt file and stops; the session translates and runs `--verify`. Twelve commands, all the session's. Good.

Only he: a request that is a customer's mail enters through `awb import --paste --kind mail` in his terminal, since
the session may not see the signature. And the word "go" for the workflows, which COMMANDS.md says are started by him
or on his word.

Unclear and the one open question of this report: the client settings of the work user deny the Workflow tool
(`seal/work-claude/settings.json`). `review.js` is a saved Claude Code workflow. The docs do not say how a tier 3
review runs on the sealed host. Either it runs from his own client as the owner, which is not a Workbench session,
or it does not run and `awb review pass` refuses tier 3. The calibration run of 2026-09-25 was before the seal.

Proposal P10: decide and write down where the lens run happens. The clean form is `awb review run deliverables/FILE`,
which starts the lens agents as subprocesses of the client from inside the session, without the Workflow tool, so
the review stays in the project under the hooks. Effort L. Boundary: the lenses read the sanitised text and the
mirrors only, as today.

Silent: the voice loop. `awb voice learn DRAFT SENT` needs the sent version in the session. A sent mail carries names
and signatures, so it must pass the paste door first. The docs do not say so and the loop runs rarely.

Proposal P11: `awb voice learn` on the owner side takes the sent text from standard input, wipes it through the
intake of the project's customer and compares the copy with the draft in the project. Effort S. Boundary: the wipe
runs where it runs today and the comparison sees the copy only.

### 7. The deliverable leaves

Two paths. From the session: `awb xchg put FILE` into `from-session/<date>/<id>` of the lab bucket, after the name
check and the send gate. He gets a mail with the code, the id and the size. He opens the OBS console, finds the
folder, downloads the file, runs `awb reveal FILE --out PATH` and sends the mail from his client. From his terminal:
`awb bucket put tcp-q7m4 FILE --reveal` into `out/` of the owner bucket, with the send gate.

Only he: the reveal (the names) and the sending (his mail account). Both must stay his.

Friction: the mail names an id and never a name, by design. A deliverable with its `.de.md` and `.ru.md` is three ids
in three mails and a console visit each. The reveal is a second command with a path outside every git tree.

Proposal P12: an owner-side `awb fetch tcp-q7m4` lists what the project put since his last fetch, downloads it into a
folder of mode 700 in his home outside git, runs the reveal on each text and prints the folder. Effort M. Boundary:
the reveal stays owner-only and terminal-only, the folder is one `awb reveal` allows today.

### 8. The project is reported on and closed

The session keeps `Status:` and `Next:` in STATE.md. The ledger entry is drafted from the commits every two hours,
always with the kind `other` and the note "correct it with awb ledger add". `awb close tcp-q7m4` is refused with the
counts of open items and live resources, then `awb projects delete` removes the folder. The period report with names
is his: `--reveal --out FILE`.

Silent: the close refusal gives counts, not the items. The drafted ledger kind skews `awb report --by kind` toward
`other` until he corrects each line, which he will not do.

Proposal P13: `awb close` prints the open items and the live rows, codes only, each with its command. The draft takes
its kind from the project: `proof` for a project with a lab evidence file, `inquiry` for a query, `pricing` when a
pricebook was built. Effort S. Boundary: none.

### Across the journey

After a reboot: `awb vault unlock`, then `awb keys unlock`. Two commands and one passphrase. Until then every prompt
of every session is refused with the time of the lock. Proposal P14: `awb unlock` runs both in order. Effort S.
Boundary: the passphrase is typed and never stored.

Which window: COMMANDS.md says "your own shell" per command, but the work shell and the owner shell look the same.
Proposal P15: `awb` without arguments prints the side (owner or work), the lock state of the vault and the keys and
the one next command for this side. Effort S. Boundary: none.

## The ten changes that save the most time per week

Ordered by the time saved for one architect with five projects: how often a step recurs (the start and the exit
once per piece of work, the questions and the labs daily, the unlock per reboot) and the counts of the work profile
(a slip in 37 of 86 projects, 391 live checks, 95 translation requests).

1. P1, one owner command to start a piece of work: register, spawn, grant, import and the open line. Four commands
   in two windows become one. M.
2. P12, `awb fetch` on the owner side: download, reveal, one folder. It replaces three console visits and a reveal
   per deliverable. M.
3. P2, the held line with a class word and the right command for a missing grant. It ends the one refusal he cannot
   read. S.
4. P4, `awb projects open` as a numbered list with the `Next:` line of each project. S.
5. P10, the lens run as one command inside the session and a decision where it runs on the sealed host. Without it
   tier 3 is blocked or runs outside the seal. L.
6. P14, `awb unlock`. S.
7. P7, `awb lab run` without `--project` and with a default tenant. S.
8. P5 and P6, Russian forms at the first import and a code lookup on the owner side. Every dictated name that is
   refused costs a window switch today. S.
9. P8, a mail when a session leaves a command for him in OPEN.md. He stops reading five sessions to find the one
   that waits. S.
10. P13, a close that lists what is left and a ledger draft with the right kind. S.

P3, P9, P11 and P15 follow when these are in.
