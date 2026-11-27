# The architect's guide

What the Workbench does for an architect and the commands of the daily work. Back to the [README](../README.md).

## What it does for an architect

**What-if questions.** What happens if the database moves to another region? If a partner runs ten customers in one
tenant? The answer is built from checked facts, each with its source. The model's memory is not a source.

**It remembers the platform so you do not have to.** You no longer keep in your head which endpoint a service has in
which region, how a call is signed, which project id goes into the path or how a list is paged. The knowledge base
keeps these details as checked facts, many of them proven with a live call on a tenant: the calls that work with
their parameters, the ones that do not and why, what differs between regions. The API client builds the endpoint,
signs the request, finds the project id and tells an empty answer from one it could not read.

**Facts with a weight.** Much of what we know about a platform comes from old slides and from what someone said in
a meeting. In the Workbench every fact carries its weight:

| grade | meaning |
|---|---|
| `live` | checked on the platform itself |
| `contract` | from the service description |
| `docs` | from the documentation |
| `said` | a statement of the product team |
| `assumed` | not checked yet, used as a lead only |

A negative ("this is not possible") needs two recorded attempts. Facts that go stale (availability, flavors,
regions) carry an expiry date and are checked again by the fact-check service: a check block on the fact says
which calls or which recipe prove it, what confirms it, what refutes it and the control that tells the two apart.
A read tier runs every Saturday evening as the work user, a write tier runs a recipe on the test tenant on the owner's word
and a watchdog deletes what a run left behind. A run never raises a grade; a refuted fact stays expired for a
person.

**Only what is offered.** A service counts as offered only when the latest service description lists it. The docs,
an API that answers or a price record do not make it offered. `awb service check` tells, both chats carry the list
and the review refuses a deliverable that names a service which is not offered.

**Whole-solution prices.** Anyone can look up one price in a calculator. The Workbench prices the whole solution:
every position fetched live from the public price API and a price sheet recomputed to the cent before it leaves.
Every total, sum and saving is computed by `awb calc` with exact decimals. The model never adds numbers in its head.
The review accepts a computed number only together with its recorded calculation.

**A price workbook.** `awb pricebook build estimate.txt --lang de` takes a specification of line items (service,
flavor, region, quantity, hours, pricing mode and an assumption per line), refuses a service that is not offered,
prices every line live and writes an Excel workbook with the figures as formulas, the assumption of each line in its
own column, a sheet of sources (snapshot time, region, service description revision) and a Markdown summary to paste
into a mail. `--against` an older version adds a sheet of what changed; `awb pricebook diff` shows it as text.
A line item with `mode: pool`, `pooled_hours: H` and `term: r24` (optional `start:`) is Reserved for many machines
that each run part time: the workbook gets two lines, the Reserved units n chosen over the term and the Open Elastic
rest hours above them, with an assumption that names the pooled hours, the month model and the open minimum order
volume.

**Reserved for many machines.** Reserved is a monthly contingent per billing item (flavor plus OS), not per instance.
`awb price pool ecs --flavor F --os Windows --hours 2720 --term R24` fetches the record live and counts the units n
once over the term from the pooled hours of all instances (730 h months or the calendar months from `--start`): the
monthly figures, the term total against Open Elastic, the threshold R / PAYG and the open points (minimum order
volume, lead time, the stopped-GPU question for a GPU flavor). A term with the rate 0 is not offered. `awb price
check` notes the pool rule on a sheet with a reserved line and warns when the units equal the instance count.

**Migrations in phases.** `awb migrate` takes an Azure inventory (a sanitised copy of the customer's spreadsheet) to
a reviewed plan: discover, map, estimate, plan, review. Every machine gets the nearest TCP flavor that is not smaller
on vCPU and memory, from the live price API, with a reason for every row that needs a decision. Each phase is done
by a worker with only the rights it needs and passes a gate before the next one starts.

**Reviewed before it leaves.** `awb review` turns a deliverable into a list of claims, each with its evidence. For an
important text a review board reads it as an architect of the source platform, an architect of TCP and the partner
who has to sell it, plus a check for invented facts and a misread question. The answer is rewritten from the
question, never patched. A review runs only on his word and a put never waits for it. There are two levels:
"ревью" (review) runs the lenses and the request check on the session's own model, findings only; "глубокое
ревью" (deep review, also "final check") runs one strong model after one word of his to the cost, which reads the
request, the evidence and the review again and writes the answer once more, with every caveat of the research kept
(`awb review show` lists the run). The final text then needs only the platform and partner lenses (`after_fx`)
while it stays unchanged.

**Live examples of our own lab runs.** A project keeps full API requests and responses of the lab tenants with
every id: domains, projects, users, groups, resources, request ids, the header and claims of a JWT. Only passwords
and logins are masked; temporary tokens and keys are shortened to 8 characters and their length; a permanent key
is never written. A file written inside a project stays as written: the write hook runs no secret check, no name
check and no mask there (DMZ 2). `awb paste --own` does the shortening for a pasted output. The commit gate of a
project, an ask project included, runs no content check (DMZ 3, below): a name, an IP address, a UUID and a key all
pass. Customer names never enter: the entry checks are unchanged.

**Texts in the architect's own voice.** `awb write check` measures a text against the architect's style and a
drafting skill writes mails, offers and PoC documents in that voice, with a check that the voice pass changed no
fact. It blocks what he struck out most: "we" for "I", jargon, bold leads, admissions of weakness, sales tone and
the history of the draft, and it names a section the request did not ask for.
A session shows him a draft only after the check ran clean in the same turn.
In a correction round the skill takes every fix of a message in one pass, fixes the master first and shows the
whole Russian reading copy again with no change list; the review runs once at the end and the length does not grow.
`awb voice learn DRAFT -` learns from the sent text piped in on the owner side: it is wiped like a paste first and
the original is written nowhere.
`awb write translate` makes the German version and his Russian reading copy from the accepted text in one step and
proves that every number, identifier and code block is kept. It keeps facts, not English spellings.
A number with its unit (12-month, 12 Monate), a date (2026-10-21, 21.10.2026) and a number format (1,340.15,
1.340,15) are the same fact in both languages. An identifier inside a hyphenated word (g7v-Host) counts. A changed
value is still refused.

**Few questions.** A session asks at most one question per reply, at its end, with the default it takes. Every other
open decision goes into the project's `OPEN.md` with its default. `awb report --by questions` counts the replies that
ended with a question.

## The main commands

The full list, with every option and who runs what, is in `COMMANDS.md`. That file and the three client hook files
(`plugin/hooks/hooks.json`, `seal/work-claude/settings.json`, `seal/work-claude/managed-settings.json`) are
generated: `COMMANDS.md` is `docs/commands-guide.md` followed by a reference taken from the argparse parsers, the hook
files are `hooks.client_settings`. After a change to a parser, a help text, the guide or the hook list run
`python tools/gen_docs.py --write`; `python tools/gen_docs.py` alone lists what is out of date and exits 1, and
`tests/test_generated_docs.py` fails while a committed file differs from the generated one.

| task | command | what you get |
|---|---|---|
| start work | `awb spawn query --goal "..."` or `awb spawn project --goal "..."` | a folder with its scope, rules, hooks and git. A query works with the knowledge base, the docs and live prices; a project also reaches the test tenants. `--customer new` on either issues `CUST-XXXX` with the four characters of the project code (`tcp-xxxx`); an existing customer is refused, every project gets its own code |
| start work in one owner command | `awb start project --goal "..." --customer new` (or no customer; `query` the same) | in your own terminal: the customer code is issued with the project code from the same four characters and its written forms are asked at the prompt as `awb import` asks (never an argument, so never in the shell history), `awb spawn` runs as the work user through `sudo -n`, your grant (`awb projects kind`, with the customer code it registered) is written, what lies in the project's `in/` folder is imported; it ends with `open: awb projects open tcp-xxxx`. With `--from DIR` (and `--trusted` for an export that carries no name) the import takes every regular file of DIR and its subfolders instead of `in/` (a link or a special file is skipped and counted) and the last line is `app: SSH host awb-10491, folder WORKHOME/tcp-xxxx` (the folder from the register). Your own assistant session (desk or development) may run it too, without `--customer` and `--review-tier` and with `--from` only beside `--trusted` (UX10) |
| switch | `awb projects kind tcp-xxxx project` (a customer project: `--customer CUST-XXXX`, the code you type from `awb register`) | a query that needs a live test becomes a project (or back). The grant records the customer and is written only when you confirm it with `--customer`; without it the command prints `grant not written`, shows the row as information and asks you to type the code yourself (no ready command), another code is refused |
| see the projects | `awb projects list` | every project with its goal and the session that holds it |
| park a project | `awb projects park tcp-xxxx --reason "waits on the customer"`, later `awb projects unpark tcp-xxxx` | the board counts it apart and leaves its open items out; nothing else changes, material still comes in |
| finish a project | `awb close tcp-xxxx`, then `awb projects delete tcp-xxxx` | close waits for open items, live resources and kept lab buckets (one `awb lab kept drop NAME` line each) and lists them with their commands when it refuses, runs `awb lessons distill` once more (as the work user; for anyone else one line says it did not), then writes the fingerprint of the deliverables to the shared side and pins its hash in the vault; a session that leaves you an item with an allowed `awb` command in OPEN.md gets you one mail, sent by the 15 minute timer of `awb build nudge`; delete removes the folder and keeps the code |
| take a file | `awb inbox take the pdf` | the file you described, from either inbox, into the project |
| look up a fact | `awb kb find "ecs flavor eu-nl"`, `awb kb show KB-XXXX` | the first 20 ranked facts (`--top N`, `--all`) with grade, source and check date; `show` also prints how the fact is checked again (its check block) |
| act on an expired or duplicate fact | `awb kb expired [--days N]`, `awb kb renew KB-XXXX --source URL`, `awb kb retire KB-XXXX --why "..."`, `awb kb merge KEEP DROP` | renew when it still holds where you read it, retire when it no longer does, merge two entries that say the same; each runs the checks of `amend` |
| remove a retired fact of another scope for good | `awb kb purge KB-XXXX... --owner-word "..."` | only a retired entry of scope hcs, hci or hce (no HCS, HCI or HCE knowledge on the TCP side): an active or a tcp entry is refused and nothing goes. The ids are written to `entries/PURGED` and never drawn again, INDEX.md is rebuilt and the command commits those paths; the pre-commit check lets a deleted entry file pass only with its line in `entries/PURGED` of the same commit |
| a pattern card | `awb kb pattern add --package cad-on-tcp --kind sizing-rule --tag gpu --fact KB-XXXX "rule"` in the source project | a reusable piece for solution packages; refused with class and count when it carries a price or one of the project's own strings (no name check inside); `awb kb pattern support PAT-XXXX` counts another project for it; `--kind lesson --sub dead-end\|working-path\|gotcha\|decision` (a `decision` also `--why`; recommended, without it a lesson is a `gotcha` and the command says so) writes a lead graded `said`, best before 90 days after its date, which `find` and `list` leave out afterwards unless `--all` |
| lessons of a project | nothing: the stop hook asks once per batch after a step that failed and then worked, a reverted step or a new decision in STATE.md and the first prompt after a compaction asks once; `awb lessons distill tcp-xxxx` as the work user in the project | the session writes lesson cards (`awb kb pattern add --kind lesson`) or says there is nothing; distill reads the transcripts of this project's own sessions only, writes `notes/lessons-digest.md` with every word that is not plain English or a TCP term as [x] and starts the side session `lessons` on a cheap model that turns it into lesson cards; `awb close` runs it once more before the close |
| a solution package | `awb solutions build cad-on-tcp`, `awb solutions status`, `awb solutions release cad-on-tcp` in the solutions project | `out/vN/` with live prices, the staleness counts per package, the release screen (file, line and class only), then the tag |
| ask the projects for cards | `sudo -n -u awb -H awb solutions sweep cad-on-tcp` (the desk) | codes and counts of the projects that name the package's terms; a note or a side session for those that changed. A session that changed a deliverable whose project tags meet a package is asked once at its stop |
| check facts again | `awb kb recheck-run --tier read --tenant ALIAS` (`--dry-run` first; without `--tenant` the tenant `recheck_tenant` of the host file names; the service project is `--project`, else `recheck_project` of the host file); the read tier also runs by itself every Saturday at 19:00 UTC as the work user (`awb-kb-recheck.timer`, on the tenant of `recheck_tenant`) | the entries due (expired or expiring within 8 days, task KS) whose check block a run can send, checked on the test tenant and written back: confirmed ones are fresh again with their grade kept (a run never raises a grade; an entry whose grade is not live is listed for you), the rest stays expired and is listed in the run's RESULT.md; a refuted one is also written to `~/tcp-shared/recheck/refuted.tsv` and leaves the next dataset; an entry whose recheck the refresh requested (`~/tcp-shared/recheck/requested.tsv`) counts as due until a recheck dated on or after the request |
| prove a check block | `awb kb recheck-run --try --id KB-XXXX --tenant ALIAS [--block FILE] [--out DIR]` | the read tier of the entries named, now, due or not, with the code and the verdict rules of the weekly run: the verdict and its three test lines per entry; nothing is written back (no kb change, no line in `state.tsv`), the evidence goes to `--out` or the session scratch folder. `--block FILE` tries a block before `awb kb how` attaches it |
| clean up after a check | nothing: `awb-kb-recheck-watch.timer` runs `awb kb recheck-watch` as you every 15 minutes | every resource of the service project whose expiry has passed is deleted and proven gone (by its `awb-project` tag, or for a service whose listing carries no tags by its lab name with the code and the expiry marker; a name match alone is listed and left alone) and its row in RESOURCES.md says so; one mail for a resource it cannot delete and one for each recheck run that ended |
| offered or not | `awb service check dms rds` | the section and revision of the latest service description |
| a console reference entry | `awb mirror console find "create vpc" --service vpc`, `awb mirror console show vpc/03-create` | the private console reference (`~/tcp-mirrors/console/<date>/`, one folder per dated pass, filled by the owner; never in the public dataset or in git), ranked by word overlap over the INDEX note, menu path, title and text; a how-to keeps its sources in an internal note and writes a step the reference lacks as "not confirmed yet: check on screen"; read only; the work user reads the copy that `awb mirror console publish` (owner side) puts in `/srv/tcp-mirrors/console/<date>/`; `awb mirror status` lists the passes, the newest date, the entries and the services |
| a console how-to | `awb howto draft "create a vpc" --service vpc`, edit the text, `awb howto render deliverables/howto-create-a-vpc.md` | see "Console how-tos" in [console.md](console.md#console-how-tos) |
| a PDF of a deliverable | `awb pdf deliverables/offer.md` | `deliverables/offer.pdf` from a Markdown or HTML file, with selectable text; see "A PDF of a deliverable" below |
| a Word file of a deliverable | `awb docx deliverables/guide-en.html` | `deliverables/guide-en.docx` for partners who edit with tracked changes, the structure of the PDF; see "A Word file of a deliverable" below |
| what is new on TCP | `awb mirror rn`, then `awb refresh --part release-notes` | the release notes of T Cloud Public as a mirror (the third public source, next to the docs and the service description; a run reads at most 200 pages of at most 2 MiB each and cuts long titles and tags) and, for each note first seen since the last refresh, the knowledge entries to check again (`recheck: KB-XXXX (release note DATE SERVICE)`; only entries that name the service and share two topic words with the note, most shared first, 15 per note and `--all` for every one; a note dated after today is listed as `announced`) and the services the list does not know (`new service?`; a title or service name from the site shows as printable ASCII cut to 80 characters); every matched entry (all, not only the 15 shown) gets a recheck request in `~/tcp-shared/recheck/requested.tsv`, an announced note from its date on, which the Saturday read tier takes as due |
| the refresh every week | nothing: `awb-refresh-weekly.timer` runs `awb refresh weekly` as you every Saturday 17:00 UTC, two hours before the read tier | every part with the update of the mirrors, the release notes and the prices; it requests a recheck for each entry a release note matches, for every contract entry when the service description has a new revision and for each entry whose source names a page of the docs mirror that changed (`requested.tsv`, counted by reason in the report); a run that fails shows in `awb owner status` as `refresh: weekly run failed: <reason>` until one goes through |
| read public documentation | `awb doc fetch https://learn.microsoft.com/...`, `awb doc hosts` | the page as text, whole (no withholding, no masking), with the line `source: URL, fetched YYYY-MM-DD HH:MM UTC`, fetched by the key service on the owner's side (see "Public documentation through the key service" in [tenants.md](tenants.md#public-documentation-through-the-key-service)) |
| a price | `awb price find ecs --grep s3.large` | the live records with every term |
| check a price sheet | `awb price check sheet.csv` | every price fetched again, every total recomputed |
| Reserved for many machines | `awb price pool ecs --flavor F --os Windows --hours 2720` | the units of one billing item chosen over the term, against Open Elastic |
| a price workbook | `awb pricebook build estimate.txt --lang de` | the workbook (.xlsx) and a summary (.md) for a mail; `awb pricebook diff OLD NEW` compares versions |
| compute | `awb calc "730 * 0.0418 * 3" --places 2` | the exact result, recorded as `K-N` for the review |
| the test tenants | `awb cloud tenants`, `awb tenant now test-1` | the tenants by alias and what runs there |
| call the TCP API | `awb cloud call GET vpc "/v1/{project_id}/vpcs" --tenant test-1` | the answer, signed by the key service (an OBS list such as `GET obs /` whole as data); a write needs `--role lab` and runs for the project of the folder; `--header Client-Request-Id=ID` adds a request header |
| a live check in one command | `awb lab run ecs-boot-ssh --project tcp-q7m4 --tenant test-1` | raise, check, evidence in `evidence/`, always torn down and proven gone (in the project's folder `--project` and `--tenant` default from SCOPE.md); `awb lab list` and `awb lab dry-run RECIPE` first |
| a migration | `awb migrate init --from azure`, then `awb migrate next` | the next phase, its worker and its files |
| review a text | `awb review init`, `claims`, `l0`, `pass` | the claim list with evidence and a review record; a translation named `<file>.de.md`, `.en.md` or `.ru.md` is accepted |
| the week | `awb ledger add ...`, `awb report --by customer` | one line per piece of work, reports by customer, technology or project |
| the half-year | `awb report --period --from 2026-04-01 --to 2026-09-30` | every project with activity in three blocks: active customer work, finished customer work, technical questions; `--reveal --out FILE` puts the names back, owner only |
| the files of the projects | `sudo -n -u awb -H awb files inventory --host`, `awb files inventory --buckets` | one TSV per side in `~/tcp-shared/files/` with path, size, time, sha256 and md5 of every file of the project folders or of the two buckets, never the content; a path the name check finds anything in is `[withheld]` |
| the move plan of the files | `awb files plan --put` | `~/tcp-shared/files/plan-<stamp>/PLAN.md` and `PLAN.tsv` from the three inventories: identical laptop copies and older versions to `_archive`, the newest into `YYYY-MM/tcp-xxxx/private/`, `clean/` or `out/`, host duplicates listed, every bucket object with its size and one action (`keep`, `move` of a loose object into `in/` or `out/` of its project, `unsorted` to `_unsorted/<stamp>/`, `archive` of a put fetched 30 days or more ago or of an older version in `out/`, `delete` only for an identical copy in one place or an object of `in/` the import took, `withheld`), sums per project and bucket at the end of `PLAN.md`; the plan moves and deletes nothing; `--put` sends the laptop rows to `reports/plan/<stamp>/PLAN.tsv` for the laptop |
| tidy the buckets | `awb files tidy --go ~/tcp-shared/files/plan-<stamp>/ --dry-run`, then without `--dry-run` | the bucket rows of the plan carried out after your `yes`: moves copied, compared and their source deleted, deletes only with an identical keeper or as `stale-in`; see "Tidying the buckets" in [install.md](install.md#tidying-the-buckets) |
| the report workbook | `awb report --codes --put` | `projects.csv` for your workbook on the laptop: one row per project, six columns (project code, customer code, task description, next step, whose move, date of last change), codes only, every text checked again; with `--put` it goes to `reports/` of the owner bucket; on the sealed host the work user's timer `awb-report-codes-work` reads the projects (`--to-shared`) and your timer `awb-report-codes` checks and puts what it handed over, every hour (`--from-shared --put --if-changed`) |
| the status | `awb board show`, `awb board write` | the status of every active project on one page, parked projects in a short section after them |
| bring a commit live | `sudo awb deploy` | the new release, only the changed services restarted, the console page `awb/tcp/web/portal.html` published when it changed, the status at the end |
| publish the console page by hand | `sudo awb web publish FILE`, `sudo awb web publish --rollback` | a checked page of your own live or a backup back, the old one kept as a backup; the deploy publishes the page of the repository |
| a login for the console | `sudo awb web user add LOGIN [--level owner]` | a random password, shown once (an owner entry also gets a TOTP key for his phone); `reset`, `remove`, `list`; `projects LOGIN CODE...` limits a reader to those project codes (no list: every project); `sudo awb web status` shows failed sign-ins and waits |
| the owner host name | `sudo awb web host HOST` | once: the host name of the owner level, kept in `/etc/awb/paths.conf` |
| what waits on the owner | `awb owner status` | one list of everything that waits on you: the owner items of OPEN.md of the active projects (parked projects as a count), the rows of `queue.tsv` whose `who` is `owner`, with their next step, the build tasks that wait on you, a failed weekly publish of the dataset, a failed put of the codes file of the report workbook and every open window of a Windows lab machine and every registered lab machine that is online; one line each with the project code or label, a few words, where it is done (project session, his terminal, steering session) and a ready command block (a command counts only in backticks after run, type or his terminal in the same item, and only then the place is his terminal); the list shows decisions only: the creates kept by lab runs are one line per project (`N creates kept on test-NNNNN, listed in RESOURCES.md`), a build task you stopped yourself or that ended with success is no item, items that start with `owner (tooling)`, `(tooling)`, `Steering:`, `allow documentation host` or `allow the documentation host` (after a leading `[ ]`, a date YYYY-MM-DD and `owner:`) are left out and printed by `--dev`, and a settled item (it starts with `[x]` or sits under a closed heading, as `awb open sweep close` leaves it) is no owner item at all, and at most five owner items show per project, oldest first, then `and N more: awb owner status --all CODE`; an owner item with no `Default:`, no backticked text and none of the words console, mail, terminal, call or meeting is one he cannot act on: it prints as `CODE  TEXT  (unclear, the project rewrites it)  [where]`, the mail counts such items in a line under its first line and the wait history leaves them out; `--all [CODE]` prints every item as it is; `--mail` sends it (the timer `awb-owner-mail` does at 08:00 Europe/Berlin, no mail for an empty list); the line of a weekly publish of the last 24 hours comes after the list and is mailed even when nothing waits on you |
| the owner level's status | `awb owner status --json` | what the owner page shows: vault, key service, release, page, UI runs (with each run's summary after the name check, for `GET /api/ui-runs`), intake counts and checked project decisions with their original item indexes, default flags and app lines; `--write` writes it for owner-actions, a timer runs that every minute |
| side work in a session of its own | `awb side start NAME --brief FILE [--model] [--effort]`, `awb side status`, `awb side take NAME [--merge]` | a project session hands a report, research or an analysis to a headless side session in the worktree `.side/NAME`, with its own brief and the model and effort it chose; the same rules and hooks apply, except that the stop hook asks a side session for no update of STATE.md and OPEN.md (both belong to the main session); the result comes back as `notes/side-NAME.md` (only one the side session wrote, never the `SIDE-RESULT.md` of the start commit) and, with `--merge`, the commits of `side/NAME` with the result committed as `notes/side-NAME.md` and never `SIDE-RESULT.md`; `awb calc` in a side session records into `calc/calc.tsv` of the project |
| read the Codex quota | `awb codex usage` | the 5-hour and weekly window, the plan and the reset credits; no tokens spent, no account data shown |
| hand a bounded code task to Codex | `awb codex run ID.txt`, `awb codex status` | the task checked (`Paths:` and `Acceptance:` lines needed), a worktree on `codex/ID` and `ID/result.md` with the diff stat and any file outside `Paths:`; nothing committed, the steering session reviews and commits |
| hand a UI task to Codex | `awb ui submit presentations/ui-tasks/ID.txt`, `awb ui status` | the task checked and queued; a result note in `presentations/ui-tasks/` when the run ends |

The owner page lists these items under **Decisions** through `GET /api/owner/items`. **Take the default** sends
`{code, item}` to the UX20 endpoint once per click, disables the button during the request and after success, and
shows `sent to the session`. A refusal shows its reason and allows retry. Items without a default show the checked
UX18 app line without a button. The owner-actions service reads only the timer status file; its access is unchanged.

Each item of `awb owner status` ends with `waits N h` (`<1 h` below an hour): the time since the wait history first
saw it. The history is `/var/lib/awb-report-codes/waits.json`, the owner's: every run of `awb owner status` and the
hourly `awb owner status --record` (after the put of `awb-report-codes.service`) note per item a key (a hash of the
project code and the item text, never the text), where it is done, first seen and last seen; an item gone from the
list is closed with the time of the run that missed it. Entries older than 60 days go. `awb owner status --history
CODE` lists the waits of one project, closed and open: start, end, hours, where. The record also writes the
codes-only summary `tcp-shared/owner-waits/waits.tsv` (code, open count, first seen of the oldest) for the column
"Waits on you" of `awb board show`.

`awb owner status` also lists each `export-*` folder directly in the owner's home whose newest regular file is
younger than 30 days and none of whose files matches an imported original by SHA-256. Files are counted
recursively; links are skipped. Import history is read from the private intake reports and the originals they
record, including sealed originals through the vault daemon. The line is `export NAME: N files, not imported
[his terminal]`. Only the folder name and count appear, never file names or content. A registered name or
secret in the folder name withholds the item and its command. A matching open queue row supplies the project
code before the folder name does: `awb import CODE --trusted DIR`. Without an unambiguous code, the command is
`awb start project --from DIR --trusted --goal "..."`. A folder with any imported file is omitted; unreadable
import history gives no export claims.

## A PDF of a deliverable

The host has no office suite, so `awb pdf FILE [--out PDF] [--title T] [--replace] [--h1-page]` is the one route from a Markdown or HTML
deliverable to a PDF, for every project and for you; no project writes its own. It writes next to FILE unless
`--out` names another file (inside the project folder or the session scratch folder only) and runs as the work
user in a project session. The PDF is A4 with the standard fonts, so the text stays selectable: headings,
paragraphs, lists, code blocks in Courier that keep their lines and indents (a block whose longest line is wider
than the page gets a smaller font, down to 6 pt, until that line fits, so a command copied from the PDF stays one
line; only a line too long even at 6 pt goes on below with a drawn arrow, never cut; a block that fits a page goes on one page, to the next one when it does not fit where it starts, so no page number line lands inside it, and a longer block breaks between its lines, never inside a wrapped line), Markdown pipe tables as a ruled grid (a bold header
row repeated on every page, column widths fitted to the page, text wrapped inside its cell, the alignment of the
separator row), HTML tables as aligned text, links with their URL, page
numbers and the title (the first heading unless `--title`) in the document info. A line `<!-- toc -->` in the
Markdown (a comment, so other readers show nothing; the same comment in HTML) puts a contents table there: each h1
and h2 after it with a dot leader and the page it starts on, found in two passes. `--h1-page` starts a new page
before each level-1 heading, except one at the top of a page; without it the headings run on as before. Text outside the standard fonts
(Cyrillic for example) embeds a TrueType font of the host by default (Liberation Sans and Mono, the metrics of the
standard fonts, else DejaVu; `awb/pdffont.py`), only the glyphs the document uses and with a ToUnicode map, so
Russian, German umlauts and Latin share one document and pdftotext gives the text back as it was written.
`--unicode` is still taken and changes nothing on its own. A character no font can show is never dropped silently:
the command refuses with its line and the count. `--replace` writes `?` for such a character and says how many;
alone it keeps the standard fonts, so every character outside WinAnsi becomes `?`. `awb howto render --pdf` and the HOW-TO.pdf of the dataset
use the same writer (`awb/pdf.py`). A project that misses something here (another file format, a font, a package)
asks with `awb dev ask` and never builds its own writer or installs a package: development decides what is installed
for all projects.

## A Word file of a deliverable

Partners who edit a guide with tracked changes need Word, so `awb docx FILE [--out DOCX] [--title T] [--h1-page]`
writes a `.docx` of the same Markdown or HTML deliverable, with the structure of its PDF and with the standard
library only (`awb/docx.py`). It reads the file with the parser of `awb pdf` and keeps its folder and link rules (no content check). Headings are the Word styles Heading 1 to 3 in the colour of the page; numbers in the headings that
count up without a gap become a Word numbering, so a heading a partner adds is numbered by Word. A line
`<!-- toc -->` becomes a Word contents field that already lists the headings and is updated by Word on open (page
numbers then). A PNG or JPEG is an inline picture with its own bytes, an inline SVG an SVG picture with a PNG
fallback drawn through `awb pdf` and pdftoppm (Word 2016 and later show the SVG, older readers the PNG; without
pdftoppm the SVG stands alone and one line says so). Captions stand centred below their figures, tables have a
header row repeated on each page, code blocks keep their lines in the style Code, lists are real Word lists, ids
are bookmarks and `href="#id"` links go to them, and the footer shows page / pages. Everything is plain editable
text: no text box, no frame. `awb xchg put FILE.docx --kind guide` sends it as before.
