# The owner's guide

The owner is the person who runs the host: the one who keeps the register of names, the tenant keys and the originals. This page is the daily work on that side. Back to the [README](../README.md).

## Start here (the owner)

```bash
awb unlock                                   # after a reboot: the vault, then the keys, each asks its passphrase
awb                                          # which side you are on, what is locked, the first item that waits on you
awb start project --goal "..." --customer new   # a new piece of work: code, forms, spawn, grant and import in one command
awb start project --goal "..." --customer new --from DIR --trusted   # the same, the import takes every file of DIR and its subfolders; ends with the app line
awb projects open                            # the active projects numbered with their Next line; the number opens one
awb fetch tcp-xxxx                           # what a project put for you, downloaded with the names back
```

## Three kinds of work

Every piece of work starts with `awb spawn` (or `awb start` on the owner side, which wraps it) and gets its own
folder with its goal, its rules, its checkpoints and
its git history. The review tier of the project is set once: `--review-tier 1|2|3` of `awb start` and `awb spawn`
(default 3) writes `- review-tier:` into its SCOPE.md, a deliverable takes it unless you raise it for that file, and
`awb projects tier tcp-xxxx 2` changes it later, as the owner. A review runs only when he asks for it; a put never waits for it (task GT).

| kind | for | reaches |
|---|---|---|
| query | a question: can TCP do this, what does it cost, how does a service behave | the knowledge base, the documentation, the service description, live prices, `awb calc` |
| project | a proof of concept, a lab, a migration plan | everything a query reaches, plus the test tenants |
| ask | the owner's standing question session on how TCP behaves | the knowledge base, the documentation and read calls on the test tenants |

A query that turns out to need a live test becomes a project with `awb projects kind tcp-xxxx project`. A query
never touches a tenant: the key service refuses its calls. A session's switch changes its row only; the key service
also wants the owner's grant, which the same command writes when the owner runs it in his own terminal. The grant
also records the customer of the project's row at that moment (a CUST code or none). From then on the key service
takes the customer for an owner-inbox take, a take from the project's folder and the
lease from the grant alone: a row that names another customer is refused in one line with the project code. A
project granted before 2026-10-10 gets its customer into the grant when the owner runs `awb projects kind` for it
once more; `awb projects check`, run by the owner, lists those and every row that differs from its grant. A
project the owner never granted has no customer for the key service, whatever its row says: its owner-inbox take
holds and its take from its own folder runs under the project code. So the owner
runs `awb projects kind CODE query` once for a customer project before its first take from the owner inbox.

An ask project (`awb start ask --goal "..."`, his own terminal or his assistant session) is the one place where only the
owner types. It never has a customer and no material ever enters it: `awb import`, the paste door, `awb paste`, `awb
material`, `awb inbox take`, `awb xchg`, `awb bucket pull` and `awb fetch` refuse it with one line. In return the
draft gate of the stop hook looks for secrets only and the commit gate of its repository runs no content check (no
hook and no commit gate reads content inside a project, below), and the tool guard lets a shell path it cannot read pass
while every refused place (another project, a key folder, the client's folder, the session records) still refuses.
The session answers from the facts first (`awb kb find`), then the public docs (`awb doc fetch`), then a call on a
test tenant (`awb cloud call`); it names the source of each answer and writes a verified
finding that is not yet in the facts with `awb kb add`, which checks the form, the language and the price and no content (the
dataset build checks every fact at the exit, because the facts are published). It reaches the test tenants directly, read and lab role, writes and lab runs (owner's word of
2026-11-24); a customer tenant, a read-only tenant and another project's code are refused by the key service.
The key service takes the ask project from the key token of the folder, never from the code a request names, so a
material op of that token is refused for any code it names.
The kind that lifts a check comes from the owner's grant alone: `awb projects kind CODE ask` in his terminal records
ask and the key token of the folder, and the hooks of the work user ask the key service (op `kind`) for the token
their folder holds. A row of projects.tsv or a SCOPE.md that says ask only adds refusals; without an answer every
check runs. The switch to ask is refused for a project with a customer or with anything in input/.

## A typical day

1. `awb spawn project --goal "Test a virtual firewall appliance on TCP"` and open a Claude Code session in the new
   folder.
2. Drop the vendor image or the customer's spreadsheet into an inbox and tell the session which file you mean. It
   takes the file with `awb inbox take`.
3. Ask in plain words. The session reads the project's SCOPE, STATE and OPEN files, looks facts up with `awb kb`,
   prices with `awb price` and `awb calc` and writes what it found to `evidence/`, with a source for each statement.
4. When it builds on a tenant, it calls through the key service with the project code. Every resource goes into
   `RESOURCES.md`. A check that a recipe covers runs as `awb lab run RECIPE`, with its evidence and its teardown.
5. A text for a customer or a partner goes through the drafting skill and `awb review`. Only the reviewed version
   leaves.
6. The session writes its ledger entry, commits and harvests by itself: what it learned live on the platform goes
   into the knowledge base with the grade `live`. When it called the TCP API and added nothing, the stop hook sends
   it back once to do so. After a step that failed and then worked, a reverted step or a new decision and after a
   compaction it is asked once for lesson cards. It keeps the two status lines of `STATE.md` current for the board.
   At the end, `awb close` checks that nothing is left running and distills the lessons once more.

## Requests to development

From a project session, run `awb dev ask "TEXT" [--blocks]`; in a side session (`awb side start`, the worktree
`.side/NAME`) it writes the ask of the project the worktree belongs to. It writes
`<shared>/notices/dev/<YYYY-MM-DD>-<code>-<n>.md` with the project code, UTC creation time,
text, command and error. Supply failure details with `--command "COMMAND" --error "ERROR"`;
without those options the fields say `not supplied`. The command is recorded without being run.
A failing lab call goes in with `--run RUN [--call N]`: `awb dev ask TEXT --run RUN` reads call N of the run log
`notes/lab-runs/<run>.log` of the project (default: the last call with a status of 400 or more; a log that left git
is read from `full/`) and adds a `## Call` section with the method, the service, the path with every id written as
`{id}`, the keys of the request body (never a value) and the status, `error_code`, `error_msg`, `code` and `message`
of the answer. The section passes the same line check as the text; a value that fails it is dropped and its key is
named. A run that is not of this project or a call that is not in the log refuses the ask in one line.
`--blocks` marks a blocked project task. The first line of TEXT becomes the title (at most 120 characters).
The text is written as it is (DMZ 4: no name, data or secret check inside); a control character refuses the ask.

An owner-side session in this repository sees `request from tcp-xxxx: TITLE (notices/dev/FILE)`
once per ask per session at its next prompt, while the ask is new (its first 24 hours). An older open ask is not
shown at the prompt again; it stays in `awb owner status --dev`, the hand-over and the console. `awb owner status --dev` lists open asks with their
file handles. After 24 hours without an answer, the default status shows one line per project:
`development owes tcp-xxxx an answer`.

Development answers through the existing note path, as the work user on a sealed host:

```sh
awb projects note tcp-xxxx --answers ASK_FILE --title "filter ready" ANSWER_FILE
```

ASK_FILE is the ask's full path, `notices/dev/FILE` or its basename. ANSWER_FILE contains the answer;
use `-` to read it from standard input. The answer title and text are written as they are (DMZ 4: no content check inside).
Only a successfully delivered note closes the matching project's ask, removing it from `--dev`
and overdue reminders. The project prompt shows the answer note once through the existing note notice.

A commit closes an ask by itself when its message names the ask's handle, the file name without `.md`
(`Q-0042: docs filter (2026-11-19-tcp-xxxx-5)`). The deploy plan lists the open asks a commit of the target names.
A deploy that ends well runs `awb dev close --release COMMIT HANDLE...` as the work user: each asking project gets
the note "live in release X" and the ask gets `"state": "closed"` with the `release`. An ask that is closed already
is passed over. A failed close is one line of the deploy's output and never fails the deploy. When the ask carries a
command recorded with `--command`, the note adds `Run the command you recorded again: COMMAND`.

When the release did not fix it, run `awb dev reopen HANDLE --error "TEXT"` in the asking project: the closed ask goes
back to open (it shows in `awb dev status` and `awb owner status --dev` again) with `reopened` counting the takes back,
`reopened_at` and `reopened_release`, the release it failed on. The ask file gets a `## Reopened N` section with the
error and the ask starts again in the events file, so the thread and the release stay. An open ask and an ask of
another project are refused in one line. `awb dev status` shows `reopened N` after such an ask.

### The owner's word and the live release

A note written on his word carries his words: `awb projects note ... --owner-word "TEXT"` (and the same option of
`awb desk close`) ends the note with the line `owner's word: "TEXT", 2026-11-20T09:30Z` (UTC, to the minute). His
words are written as he said them (one line, no control character). The project rules and the development rules take a note with this line
as his decision: the session acts on it and does not ask him again.

`awb version live` prints the live release from the deploy journal `/opt/tcp-awb/DEPLOYED` (or, without a journal,
from the installed source link `/opt/tcp-awb/src`). `awb version live COMMIT` says whether that commit (7 to 40 hex
characters) is part of it: exit 0 live, 1 not live yet or unknown. A deploy that is running or left a unit pending
counts nothing as live. A session checks this itself instead of asking him whether a fix is out.

## Requests to the desk

From a project session, run `awb desk ask "TEXT" [--blocks]` for desk work: DNS records, Cloudflare, EIPs between
projects, tenant steps. It writes `<shared>/notices/desk/<YYYY-MM-DD>-<code>-<n>.md` with the header of a dev ask,
under the same rule (written as it is). A desk session (an owner session outside a project and outside a checkout of
this repository) sees `desk request from tcp-xxxx: TITLE (notices/desk/FILE, awb desk close HANDLE --note TEXT)`
once per ask per session at its next prompt, while the ask is new. `awb desk list` lists the open asks with their
handles. The desk answers as the work user on a sealed host:

```sh
awb desk close HANDLE --note "both records are in" [--owner-word "TEXT"]
```

The note goes into the asking project (`notes/from-dev/`, title `desk answer: TITLE`; `--note -` reads standard
input) and its next session prompt shows it; only then the ask is marked closed.

## Counted frictions

The hooks count frictions into `<shared>/ux/events.tsv` (see "Customer data" in [dmz.md](dmz.md#customer-data)); `awb ux` adds and reads them, for
both users:

```sh
awb ux note "the dictation gate asked twice for one prompt"
awb ux report --since 2026-11-02
```

`note` writes one line of class `note` with the text as written (a control character refuses, as a dev ask
does) and the project code of the folder it runs in. `report` prints one row per class with its count in each ISO
week from `--since` (default: the last four weeks) and the change of the current week against the week before, then
the counts of the held stops by source and then the notes of the period. A held stop (`held-draft` or `held-note`)
carries as its text the fixed tokens of the messages that held it, joined by "+" (`ledger`, `harvest`, `contrib`,
`lessons`, `late`, `drafts`, `open`, `next-step`; a stop held by the late status and the open sweep writes
`late+open`); the report prints one row "held by TOKEN: N" per token under the table. A token outside the list is
refused, and a prompt or compaction line keeps "-". On a Monday `awb owner status` and its mail end with five lines on the week that ended
against the week before. The folder `ux` is 2775 and the file 664, so both sides append to it; each line is one
append under a lock. The phrase list `rules/ux-phrases.tsv` is a protected path.

## The open work queue

Keep the queue: use `awb queue add`, `awb queue next` and `awb queue done` whenever work starts,
its next step changes or it finishes. Review `awb queue list` when resuming work; `--mine` shows only
items assigned to `owner`. Use codes and session handles in every field.

```sh
awb queue add "check the export" --next "review the result" --who owner
awb queue next Q-0001 "send the export" --who desk
awb queue done Q-0001
awb queue list --mine
```

`--who` accepts `owner`, `desk`, `development` or `session:CODE`; add also accepts `--project CODE`.
The owner-side file is `~/tcp-awb/presentations/work-profile/queue.tsv`, one open item per row with
an id, creation and update dates, the date the next step changed, what, next, who and optional project.
`done` moves an item to `queue-done.tsv` beside it. A next step unchanged for 24 hours is marked `stale`;
reassigning the same step does not reset that clock. `awb owner status` includes owner rows and their next steps.
A row is kept as written (DMZ 4: no name, data or secret check inside, so no verdict file and no register stamp);
only its shape is checked. `AWB_WORK_QUEUE` selects another queue file, including for temporary tests.

Run `awb queue migrate` once on the owner host to import the open rows of the old `QUEUE.md` beside the TSV.
The old text stays intact with an archive notice prepended. Completed, accepted, cancelled and superseded
rows stay only in that archive. Other rows stay open; waiting or asked rows default to `owner`, development
steps to `development` and the rest to `desk`, unless a `who` column specifies the handle.
Missing next steps become `review next step`. Imported rows use the source file's modification time,
so migration does not hide stale work. Rerunning the migration does not duplicate rows.
Review `awb queue list` after migration and close resolved work; the host acceptance target is fewer than
40 open rows. Migration never discards unresolved work to meet that count.

## Session hand-over

`awb handover write` saves `build/HANDOVER.md` in the owner's repository checkout from open queue rows,
Git history since the last successful write, open build tasks (not done or stopped), the checks of a branch of the
last 48 hours that did not pass, held release notes, open development asks and the default owner status. It uses no model and runs no content check (DMZ 4): the file holds the lines as they are. A failure keeps the previous hand-over.

Development installs the owner's PreCompact, SessionStart and prompt hooks on his word, using
`awb.hooks.owner_client_settings("awb hook")`. Before compaction they write the file; session start
reports its path and UTC time. The prompt after the second compaction suggests a new session once.
Host settings are not installed by the command. See [COMMANDS.md](../COMMANDS.md#session-hand-over).
