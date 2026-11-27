# The board and the console

The web side: the board, the projects, Ask and the console how-tos. Back to the [README](../README.md).

## The board and the console

`GET /api/board` and `GET /api/projects/CODE` include `waiting.items` with `text` and `where` for each
waiting item, plus `waiting.total`. The existing board `waiting_on` strings remain available. Locations use
the command recognizer of `awb owner status`: an explicit run or type instruction with a backticked command
means `his terminal`; an item labelled `tooling:` or `development:` means `development`; `here:` means `here`;
otherwise it is `project session`. Classification follows the data check. Both endpoints cap the list at five.
Each project also has `app: {host, folder}` when `paths.conf` supplies `work_host`, the work user's SSH alias,
and `projects`, the absolute project root. The folder must match the verified registered project folder.
No destination is guessed when the configuration is missing. The gateway keeps forbidden projects and their
app destinations out of responses to a restricted login. `awb api check` checks the matching contract.

New owner decisions in OPEN.md carry a date: `- YYYY-MM-DD owner: TEXT. Default: X.`.
The project gate rejects a new undated item; unchanged legacy items keep their text.
Run `awb open sweep` inside the project to see a numbered table of open owner items,
ages in days (unknown for legacy undated items), text, defaults and evidence. A `taken`
receipt requires `Default taken: X.` as a complete commit-message line or a cell in a
RESOURCES.md handle row, matching the full default apart from case, whitespace and its
final period. The table shows the commit hash or resource handle and row, never the raw
evidence. Texts are shown as written (no content check inside, DMZ 4); only a control
character withholds one.

After carrying out a default, run `awb open close N [N ...] --default` with numbers from
the current sweep. This ticks the selected items, moves them under a Closed heading and
writes one ledger entry for the selection. Numbers are reassigned on the next sweep.
When more than 20 owner items remain, run the sweep and show the table once. The stop
hook asks for that line in the reply once per local calendar day per project, starting
at 21 items; its marker lives in the shared ledger's `.open-sweep/` folder.

Every project keeps its status in the first two lines of `STATE.md` (`Status:` and `Next:`). A session cannot end
a reply while its commits are newer than that status. Each of the two lines stays at most 300 characters and says no sentence twice; the stop hook holds a reply whose lines break that (with the two counts: move the detail below the two lines), `awb projects sync` lists the projects that do, and the board cuts a longer line at 300 characters with `...`.

Under the two lines `STATE.md` keeps a pinned block, the heading `## Pinned` with one line per standing fact:
`- Language:` for the replies, `- Limit:` for a standing limit he set, `- Deliverable:` with its bucket path and
`- Chain:` for a running chain. A new project starts with the block; the start of a project without one asks for it
in its checks. Before a compaction the PreCompact hook (`awb hook compact`) hands the two status lines and the block
to the summary as plain text and the start after the compaction (source `compact`) prints them again before every
file, so the session neither asks him again nor switches language. Both are shown as written, with a name, an
address or a secret in them (DMZ 2): only a bidi override withholds the block with one line. The same holds for `SCOPE.md`, `STATE.md` and `OPEN.md` at the start, the folder name and the reason a project is parked. `awb state rotate [--keep N]` moves the dated layers (`### YYYY-MM-DD ...`) older than the last N dates (3) to `history/STATE-YYYY-MM-DD.md`, leaves the top and writes the lines marked `pinned: Kind: text` into the block; a second run changes nothing. The block stays
within the first 40 lines, at most 12 lines and 1500 characters with every entry of one of the four kinds; the stop
hook holds a reply whose block breaks that, with the counts. The owner's own PreCompact hook (`pre-compact`, the
hand-over below) is a separate command, so the managed settings never run it twice. `awb board show` turns the status of every active project into
one page for management: what each project waits on, its open items, its deliverables by review state and its live
resources, with codes only. It also carries the wipe mode as the last refresh measured it: `awb refresh` runs the
red-team pack of wipe mode through the installed intake and keeps its leaks and losses in `refresh/wipe.tsv`.
`awb board write` keeps it as Markdown and as HTML that prints on A4. After the header lines it has a section "TCP
platform": the open incidents of the platform (impact, regions, since, title), the maintenances planned for the next 14
days by start and the line "status of 07.11.2026 10:00 CET" or "status not pulled since ..." once the last pull is
more than 2 hours old; "no open incident" when there is none. The data is `awb platform-status pull`, which reads the
public T-Cloud Status Dashboard (`status.otc-service.com`, no key, GET only) into `tcp-shared/platform-status/state.tsv`
and keeps the last good rows when a pull fails; `awb platform-status show` prints the section alone and `GET /api/board`
carries it as the field `platform`. The column "Waits on you" gives the hours of the oldest owner item of each
project that is still open, from the wait history of `awb owner status` (the owner's hourly timer writes a codes-only
summary to `tcp-shared/owner-waits/waits.tsv`; "waits on you: not measured since ..." once it is older than 3 hours).
A project that waits on the customer or rests is
parked with `awb projects park tcp-xxxx --reason "waits on the customer"` and comes back with `awb projects unpark`: the
board counts it apart ("N active, M parked") in its own short Parked section and leaves its OPEN.md items out of every
count and list. The items under a heading of OPEN.md that has closed, done, erledigt or abgeschlossen in it are out of
the counts of an active project as well. A parked project keeps its folder, grants and tenants, takes material, and
`awb close` still works on it.

`awb projects preflight CODE` checks the granted kind, tenant reads, free EIP/VPC/route-table quotas, hosts in
SCOPE.md and input/, and lab admin store freshness. Each line shows a code, a handle and `ok`, `missing` or
`unknown`; exit 0 means all checks passed, otherwise exit 1. It uses GET with read keys and spends no login.
Inaccessible owner grant records or store timestamps and login-backed tenants remain unknown. Hosts use stable
hashed handles so names and URLs stay out of the report. Spawn, start and unpark print the same report without
changing their operation's exit status; session-start shows non-ok lines once per session and project.
Start prints the report before its final `open:` line and, with `--from DIR`, its final `app:` line.
Unreadable or malformed evidence stays unknown, and symlinked input files and directories are not followed.

The console is the web side of the same work, behind a sign-in. Before the sign-in a request holds one of 64 slots
(at most 4 per address) and has 5 seconds for its headers and its body; a signed-in request has 20 seconds for its
body. It lists the projects with their status, files and
deliverables, shows the board and what runs on the test tenants and creates a new query or project with its bucket
folder. On the owner host it puts files into a project's folder `in/`; the session takes them with "take the files
from in". Both pages open with a health strip and a Now block (`GET /api/health`, from the console data service,
kept 15 seconds): six tiles for the vault, the key service, the build runner with its running check or task, the
live release with main and how many commits are not deployed, the free disk and whether awb-web runs the live
release (its process started after the switch of the last deploy), then what runs now: open build checks and tasks
with their start, lab windows with their end (from the notices of `awb winlab`, so a window closed early stays
listed until its end) and a deploy that runs or waits. Codes, ids and fixed words only; a reader limited to some
projects gets no strip. The owner page also lists the open dev asks (project code, title, age, blocks or not) and the
open rows of `awb queue` (id, who, what and next step, age, a stale mark after 24 hours without a new next step),
both from the status file of `awb owner status --write`, rebuilt at most every five minutes, every text through the
name check and withheld with its class on a finding. They are read only for now. `awb owner status` opens with
"Running now" (each build task, check, deploy, lab window, winlab transfer, dev ask, lab run and timer job that started and has not
ended, with its start and ETA, overdue when the ETA passed, and the vault or the key service while locked and the
Claude login of the build user while lapsed) and "Ended in the last 24 h" (one line per item, the latest end; red, stopped and needs-you in capitals and all shown, at most
5 ok lines and "and N more ended ok" for the rest),
both from the events file `<shared>/build/events.jsonl` that the runner, `sudo awb deploy`, `awb winlab`, the dev
asks, `awb lab run` and the owner's minute timer append to; the status file and `GET /api/owner/state` carry them as
`running` and `ended`. The minute timer mails one push per event that needs you (a red end, an end that needs you,
a lock, a lapsed login, a blocking dev ask answered) through the path of the 08:00 mail, each once and codes and
fixed words only. Each project has a chat that reads
its sanitised inputs. The Ask page answers
a general question in plain words only from the knowledge base, the live price API and the tenant snapshots.
Ask runs on `claude-opus-5-5` (`AWB_ASK_MODEL` changes it). Thinking is always on there, so `AWB_ASK_EFFORT` sets
how much the model thinks (`low`, `medium`, `high`, `xhigh` or `max`, default `medium`) and `AWB_ASK_MAX_TOKENS`
(default 16000) caps one reply; a reply that hits the cap is an error, never a cut answer. The project chat and
the Ask API use the same settings. A question costs roughly
5,000 to 12,000 tokens at medium (an estimate, not a measurement) because the thinking and the tool rounds are
counted. The daily budget of 40 questions and 300,000 tokens is counted the same way, so about 25 to 60 questions
fit in the tokens and the token budget now ends the day before the question count does when questions are hard.
The console reaches the same Ask through a JSON API on the main host: `POST /api/ask` with `{"question": "..."}`
answers the text, the fact ids it names, the prices it used and the questions left today; `GET /api/ask/budget`
returns the questions left. It shares the daily budget and the name check with the Ask page. A refusal says why:
`name` (a registered name: write the code), `budget` (used up until 00:00 UTC), `busy` or `error`. The contract is
`docs/api/openapi.yaml`. The project chat, the Ask API, the Ask page and `awb ask QUESTION` in the terminal run one
engine: the same model loop, the same budget (an unreadable usage record refuses) and the same check of the tool
answers and the answer.
Every door that sends or shows a text runs the same screen (`check.screen`): the Ask question, the project chat, the
working copy of an imported material and the owner status. It looks for registered names,
structured data, secrets, tokens, private keys and home paths, also behind base64 and hex. The owner status refuses
names and secrets only, the others refuse. The project read API of the console shows the files of a project as
written (DMZ 4: no content check inside). The table
"One text screen" in `INTERFACES.md` lists each door.

The console page lives in the repository as `awb/tcp/web/portal.html` and ships with `sudo awb deploy`: when its
bytes differ from the live page, the deploy publishes it through the same checks and backup as `awb web publish`
(see "Deploy" in [operations.md](operations.md#deploy)). The test `tests/test_portal_page.py` holds it to the contract: every API path it calls is in
`docs/api/openapi.yaml` and every field of `/api/board` is shown by the page or named in the test with a reason, so a
new board field the page does not show is a red test. `sudo awb web publish FILE` (a page of your own) and `sudo awb
web publish --rollback [NAME]` (back to a backup) stay for you; the next deploy that changes the page replaces either.

## Console how-tos

A how-to for the TCP console starts from the private console reference and never from memory. `awb howto draft
"TOPIC" [--service SVC]` finds the entries of the topic, orders them by service, menu path and step number and writes
`deliverables/howto-<topic>.md` in the project: a title, a short "before you start", then numbered steps. A step
names the menu path and the field and button labels as the screen spells them and nothing else: the deliverable is
text only, with no image link and no source. The sources and the date of the pass go into the internal note
`reviews/howto-<topic>/sources.md` next to the draft, which never leaves. `--step "WORDS"`, repeated, names the steps
yourself, one search each. A step the reference does not hold reads "not confirmed yet: check on screen"; the session
edits the text and never invents a label. A draft that exists is not overwritten without `--force`.

`--internal` writes the version with images for your own use: each step cites its source and embeds its image from
`deliverables/images/` (only the cited ones). `awb howto render FILE` writes one self-contained HTML file next to the
draft, a fenced code block (three backticks or tildes, with or without a language) as a code block whose lines a
reader copies as written, also one indented under a step, and refuses a draft with images unless `--internal`; an internal page says "internal, never publish" at the top
and never goes out. `awb xchg put` refuses an HTML or Markdown file that embeds or links an image unless the owner
passes `--internal-image` in his own terminal. `--pdf` also writes the PDF of the page next to it with the writer
of `awb pdf` (see "A PDF of a deliverable" in [architect-guide.md](architect-guide.md#a-pdf-of-a-deliverable)). The outward text goes out with `awb xchg put FILE` to a
customer or partner by mail.

The work user reads a copy, because the owner's home is closed to it. `awb mirror console publish` (owner side)
copies the newest pass to `/srv/tcp-mirrors/console/<date>/` (folders 0750, files 0640, group of the work user).
`seal/setup.sh` makes the folder, owned by the owner, so the work user reads it and cannot write there.
