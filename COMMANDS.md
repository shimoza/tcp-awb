# Commands

`awb` is the one command of the Architect Workbench. Exit codes: 0 ok, 1 findings or blocked, 2 usage or error.
`awb <command> --help` shows the options of any command.

Two sides. The owner side holds the register of names and the originals and is run by you in your own shell. The
work side is what Workbench sessions (and you) run inside `tcp-` projects. A name is only ever typed on the owner
side.

## Owner side (your own shell, never through a session)

| command | what it does |
|---|---|
| `awb init` | create `~/tcp-shared`, `~/tcp-vault` and an empty register (done once) |
| `awb register new CUST` | print a fresh code of a kind: CUST, PART, ORG, PERS, DOM, SITE, REF |
| `awb register new PERS --parent CUST-XXXX` | a sub code such as `CUST-XXXX-PERS-2` |
| `awb register add CUST-XXXX CUST "full company name"` | register one written form of a code; add every short form too. `-` reads the form from standard input |
| `awb register list` | codes, kinds and counts of forms, no names |
| `awb register list --forms` | the same with the written forms (vault side only) |
| `awb register retire CUST-XXXX` | retire every form of a code; the code is never reused |
| `awb vault show ~/tcp-vault/reports/CUST-XXXX/FILE.md.gpg` | the private report of an intake (the candidates with the words found) on your own terminal; refused inside an assistant session, to a pipe or a file |
| `awb register keep "phrase"` | a phrase you reviewed as harmless is no longer a name candidate |
| `awb register keep --list` | the kept phrases (vault side only) |
| `awb intake --customer CUST-XXXX FILE...` | sanitised Markdown copies to `~/tcp-shared/outbox/CUST-XXXX/`, originals into the vault. No FILE: everything in `~/tcp-vault/inbox` |
| `awb intake --customer new FILE...` | the same for a new customer, a code is created |
| `awb intake --customer CUST-XXXX --force FILE...` | write the copies even when unknown name candidates were found (after you reviewed them) |
| `awb register review CUST-XXXX` | sort the candidates of a blocked intake in one pass: an editor opens with them, one per line; mark the real names (`p` person, `o` company, `c` this customer, `s` place, `+` one more form, `-` later), every unmarked line goes to the keep list; the counts are confirmed before anything is written; your own terminal only, never inside an assistant session |
| `awb images list [CUST-XXXX]` | the pictures the intake held on the vault side (office media, pdf images, image files), per file: held, released, left out |
| `awb images release CUST-XXXX FILEID N...` | after you looked at them: copy the chosen pictures to `~/tcp-shared/outbox/CUST-XXXX/images/`; `--all` for every held picture of the file |
| `awb reveal FILE --out PATH` | put the real names back into a finished text: every code becomes its first active form; the named file gets mode 600 and never goes where a session reads |
| `awb vault status` | state of the vault: plain, locked or unlocked |
| `awb vault encrypt` | encrypt the vault once (new passphrase twice), then unlock |
| `awb vault unlock` | give the passphrase to the vault daemon, after every reboot |
| `awb vault lock` | drop the passphrase and the register from the daemon's memory |
| `awb vault reload` | hand the vault daemon to a new process of the installed code without a lock (what `awb deploy` and `systemctl reload awb-vaultd.service` run): `vault <state>, pid <n>, release <commit>`. While it runs, a write to the vault (unlock, lock, a register save, a seal) answers `busy`: the vault daemon is reloading, try again. A stop, a crash or a reboot still lock the vault |

Every `awb intake` first runs its self-test over the planted cases of `awb/planted.py` and takes nothing in
when one of them is no longer found. Every output passes two checks: the matcher and a second check by another
method (the letters of whole words against every registered form).

## The bucket (owner side)

One folder per project in the bucket `awb`: `<YYYY-MM>/<project-code>/in/` for your originals and `out/` for
results. A folder is looked for under every month and used where it is; a new one goes under the month the
project was created. A drifted folder is renamed in the bucket, never the project.

| command | what it does |
|---|---|
| `awb bucket sync` | create the folders of every active project; report folders that match no project (count per month, no name) |
| `awb bucket folder CODE` | the folder of one project, created when missing |
| `awb bucket pull CODE` | new files of `in/` into the vault inbox, then the intake for the project's customer; a file is taken once (`--again` takes it again). Your own shell |
| `awb bucket put CODE FILE` | FILE into `out/`; `--replace` overwrites, `--reveal` puts the real names back first (your own shell). The send gate runs first: for a project with a customer or partner it refuses a deliverable without a valid review at tier 3 (or a tier he lowered) and a tier 3 review without the request check; an internal file leaves with a warning |
| `awb bucket move <YYYY-MM>/<NAME>/ CODE` | rename a drifted folder: copy inside the object storage, compare names and sizes, then delete the old objects |

Set once in your shell profile: `export AWB_BUCKET_KEYS=pass:<entry of your password store>` (the entry holds
`ak` and `sk`). `AWB_BUCKET` and `AWB_BUCKET_REGION` default to `awb` and `eu-de`. The work user of the seal
cannot use the bucket, by design.

Seal, once, with sudo: `seal/setup.sh --dry-run` (read the plan), `sudo seal/setup.sh --mirrors
~/tcp-mirrors/docs ~/tcp-mirrors/service-description` (your shell expands the `~`), log in again,
`sudo seal/verify.sh` (every line PASS).

## Deploy (owner side, your own shell)

| command | what it does |
|---|---|
| `cd ~/tcp-awb && sudo awb deploy` | after a commit: bring it live, restart only what changed, ask the vault passphrase once when the vault daemon restarted, end with the status lines |
| `awb deploy --dry-run` | the plan and every step, without root; nothing runs and nothing is asked |
| `awb deploy status` | the journal, the release each daemon runs, locked or down since when, a split between a daemon and the code |
| `sudo awb deploy --only awb-vaultd.service awb-keyd.service` | these units only, also when nothing changed (after a library upgrade) |
| `sudo awb deploy --all` | restart every installed unit |
| `sudo awb deploy --rollback` | the release that was live before this one |
| `sudo awb deploy --to COMMIT` | another commit of the repository |

The tree must be clean (commit first). The deploy runs the installed copy of its own code, so a change of
`awb/deploy.py` is live from the following deploy. `seal/setup.sh` (full) stays for the first seal of a host.

## The console page (owner side, your own shell)

| Command | What it does |
|---|---|
| `sudo awb web publish --from-queue ID` | publish the page of a UI queue run: the applied source of a completed run or the candidate of a `--candidate-only` run in state ready, only with a validation that starts with PASS. Checks names, secrets, tokens, private keys, one `<title>` and no external script, keeps the current page as `index.html.before-ID`, installs the new one 644 root:root, prints the size and the backup |
| `sudo awb web publish FILE` | the same for a file; the backup is `index.html.before-<date-time>` |
| `awb web publish --dry-run --from-queue ID` | the checks only, without sudo; nothing is installed |
| `sudo awb web publish --rollback [NAME]` | put a backup back (the newest without NAME); the replaced page is kept as a backup too |
| `awb web status` | the size and time of the page, the last backup, the gateway and `/health` |

A refusal names the class of the finding (name, secret, token, private-key, html) and never the text. Under sudo the
page is read and checked as you; root only keeps the backup and installs.

## Offered services of T Cloud Public (TCP)

Offered is what the latest service description lists, nothing else. The docs, an API that answers, a price record
or a memory never make a service offered. Sessions run these as well.

| command | what it does |
|---|---|
| `awb service check NAME...` | offered or not, with the section and the revision; takes short names and the forms people write; a service the text marks no longer available counts as not offered after that date; exit 1 when one is not offered; a note when the mirror holds a newer revision than the lists |
| `awb service list` | every service of the current revision with its section, its other names and its end date |
| `awb service update` | after `awb mirror sd` brought a new revision: `rules/services.tsv` and the block of service names in `rules/stop-words.txt` rebuilt from it (owner side); the documented services the service description does not know are printed, to check each before it goes into `rules/services-not-offered.txt` by hand |

The chats get the list in their instructions and a note under an answer that names a service which is not
offered; the review refuses such a deliverable (`service:` in the findings); `awb refresh` reports lists older
than the mirror.

## Prices of T Cloud Public (TCP)

The public price API needs no key, so sessions run these as well. Every answer is records, empty (the API
answered with none, which is no proof that the item does not exist) or unknown (no usable answer). A flavor that
is not under the service asked for is looked for under every service name of the region: the ECS families sit
under six of them.

| command | what it does |
|---|---|
| `awb price find SERVICE [--flavor F] [--os TEXT] [--grep TEXT]` | records of one service in a region (`--region`, default eu-de); exit 1 when nothing matches; `--json` for a script |
| `awb price find --id ID` | one record by its id |
| `awb price snapshot [--region R]...` | keep the whole listing of a region in `~/tcp-shared/prices/tcp/<region>/<date>.json` (default: eu-de, eu-nl, eu-ch2); an empty listing is refused |
| `awb price diff [--region R]` | every price value against the last snapshot, by record id; `awb price diff OLD NEW` compares two snapshots |
| `awb price check SHEET` | a price sheet to the cent: every price fetched again, every total and the grand total recomputed, after a self-test with planted wrong rows |

A sheet is CSV or TSV with a header row and one position per row: `id` (or `service` and `flavor`, with `os` when
several OS tiers match), `region`, `term` (PAYG, R12, R24, R36, RU12, RU24, RU36), `unit_price`, `quantity`,
`hours` (for an hourly rate), `total`, `source`, `date`. A row with the id `TOTAL` carries the grand total. Numbers
take a decimal point; a comma counts only as a thousands separator next to one.

## The cloud (owner side, read only)

| command | what it does |
|---|---|
| `awb cloud projects` | how many projects the key reaches, with names and short ids |
| `awb cloud get SERVICE PATH [--query K=V]...` | one signed GET; `{project_id}` in PATH is the project named like the region |
| `awb cloud get SERVICE PATH --list KEY` | a paged list (`--paging` marker, offset or none): list, empty or unknown |
| `awb cloud sweep` | servers, disks and elastic IPs that are untagged (no `awb-project` tag), expired (`awb-expiry` passed) or idle, oldest first; every resource as a neutral handle such as `ecs-3`, the ids in `~/tcp-shared/handles.json` (mode 600) |
| `awb cloud usage [--month YYYY-MM]` | every call to the TCP API per day from the call log: count, 429 answers, errors, seconds waited, by service. Every call keeps at least 0.25 s from the one before to the same host and waits what a 429 names in Retry-After |

Set `AWB_CLOUD_KEYS=pass:<entry of your password store>` like the key of the bucket. The command line sends GET
only. The work user of the seal gets its own key with T-100.

## The refresh and the mirrors

| command | what it does |
|---|---|
| `awb refresh` | one run of the refresh with a report in `~/tcp-shared/refresh/<date>/REPORT.md`: mirrors brought up to date, a price snapshot and every value changed since the one before, the expired knowledge entries with how each can be checked again (`worklist.jsonl`) and the retrieval numbers, projects whose STATE.md is older than 14 days and stale pointers (a command the rules name that does not exist, a knowledge id the code cites that is retired, a contract entry citing an old revision). `--part P` for some parts, `--no-update` to read the mirrors only. Exit 1 when something is overdue |
| `awb refresh apply RESULTS.jsonl [--plants PLANTS.json]` | record the results of a re-check through the checks of the knowledge base: confirmed as `kb recheck`, changed as `kb amend` plus `kb recheck`, refuted as `kb retire` (and the corrected fact as a new entry), unchecked left alone. A batch that confirmed one of its planted false facts is not applied at all |
| `awb mirror docs` | clone or update every documentation repository of the public GitHub organisation into `~/tcp-mirrors/docs/` (owner side; `--budget SECONDS`, the same command resumes) |
| `awb mirror sd` | the service description into `~/tcp-mirrors/service-description/<revision>/`, PDF and text; a new revision gets its own folder |
| `awb mirror status` | what the mirrors hold |

The re-check of the worklist is agent work, run from a session on the owner side. Split `worklist.jsonl` into
batches by service and plant one false fact in each (the brief forbids looking ids up). Every first-check agent
reads `workflows/refresh-brief.md`. Every changed and refuted result goes to a second agent with
`workflows/refresh-verify.md`. Merge the two: agree applies, adjust applies the fixed fields, disagree leaves the
entry as it is. Then run `awb refresh apply` with the plants file, on a copy of the knowledge base first
(`AWB_KB=<copy>`), then for real.

The work user of the seal reads the mirrors read-only under `/srv/tcp-mirrors/`: give them to the seal with
`sudo seal/setup.sh --mirrors ~/tcp-mirrors/docs ~/tcp-mirrors/service-description`.

## Projects

| command | what it does |
|---|---|
| `awb spawn query --goal "..." [--customer CUST-XXXX] --tag iam` | a query `tcp-<code>` with its files, hooks and git: the knowledge base, the docs mirror, live prices and calculations, no test tenant (the key service refuses it) |
| `awb spawn project --goal "..." [--customer CUST-XXXX] --tag vpn` | a project: everything a query has plus the test tenants through the key service |
| `awb spawn query --goal "..." --customer CUST-XXXX --from-outbox` | the sanitised copies waiting in `~/tcp-shared/outbox/CUST-XXXX/` move into `input/` and the first commit |
| `awb projects kind tcp-xxxx project` | switch a query to a project (or back); a ledger line records it. The old kinds engagement, topic and code read as query, lab as project |
| `awb projects list` | the project register with the goal of each project (checked for names again) and the session that holds each active project; `--paths` shows the folders instead of the goals |
| `awb projects delete CODE...` | remove the folder of closed projects (`awb close` first); refused while RESOURCES.md lists a live resource or a session holds the project; the code stays registered as deleted and `awb projects list` hides it (`--all` shows it) |
| `awb projects check` | tcp- folders that no project registered (a name that is not a code shows as "a folder") |
| `awb projects sync [CODE...]` | bring the template lines of CLAUDE.md and STATE.md of every active project (or the ones named) up to the current rules: an old line is replaced, a line the template gained is added, every other line and the project's own notes stay; nothing is committed. Running sessions hear of changed rules by themselves: the prompt hook tells a session once when the rules it loaded at its start changed |
| `awb board write [--out DIR]`, `awb board show` | the status of every active project for management, on demand: the Status: and Next: lines of STATE.md with their date and time, the commits newer than them (marked, never written), what each project waits on, its first open items, its deliverables by review state and its live resources. `write` puts `board-<date>-<time>.md` and `.html` (prints on A4) into `presentations/board/`, `show` prints the Markdown; codes only. The portal shows the same live under Board (`GET /api/board`) |
| `awb close tcp-xxxx` | close a project: refused while OPEN.md holds items or RESOURCES.md lists a resource that is not deleted or kept; `--force` closes anyway and says what is left |
| `awb english list [--month YYYY-MM]` | the English notes the sessions wrote, kept after a name check; once a month, the three that keep coming back |
| `awb portal serve [--port 8080]` | a read-only web page over the knowledge, live prices, the projects and the reviews, on 127.0.0.1 only; reached through a tunnel with an access check in front, never directly |
| `awb keys unlock` | load the keys and secrets of every tenant from your password store into the key service (`awb/tenant/<alias>/ak`, `sk`, `lab/ak`, `lab/sk`, `secret/<name>`); after every start of the service. `awb keys lock` forgets them, `awb keys status` shows aliases, roles and secret names, never a value. `awb keys reload` hands the service to a new process of the installed code and keeps the keys (what `awb deploy` and `systemctl reload awb-keyd.service` run). Your own shell |
| `awb cloud call METHOD SERVICE PATH --tenant ALIAS [--role read\|lab] [--region R] [--query K=V] [--body FILE] [--project CODE]` | one call through the key service, for sessions too: the service signs, a write needs the lab key and an active project, a body may fill a password field with `{{secret:NAME}}`, the answer comes back with every key and secret taken out. `awb cloud tenants` lists the tenants |
| `awb inbox take WORDS...` | a session takes the file you described, from either inbox: your words as they are ("the pdf", "эксель", "the newest", a part of the name; `|` between several ways of saying it) are matched against the file names inside the key service, so no exact name is needed. One file found: from the lab inbox (`<lab bucket>/inbox/`) after the name check into `input/` and the project's `in/`, from the owner inbox (`awb/inbox/`) only through your intake, the session gets the sanitised copies; a hit or an unknown name holds the file and you get a mail. None or several: both inboxes are listed (id, kind, size, time; names of the owner inbox are never shown) and the session takes the one you mean with `--id ID`. `--all` takes every file of both inboxes; `--customer CUST-XXXX` for a project without a customer. `awb inbox list` shows both inboxes |
| `awb xchg put FILE [--as NAME] [--image --reason TEXT]` | a result into `<lab bucket>/<code>/from-session/<date>/` after the name check and, for a customer project, the send gate; you get a mail. `awb xchg list` |
| `~/.config/awb/keys.conf` | your settings of the exchange, sent with `awb keys unlock`: `bucket_tenant` (the alias whose lab key reaches both buckets), `lab_bucket`, `owner_bucket`, `region`, `max_mb` (default 100), `notify_topic` (an SMN topic urn for the mails) |
| `awb tenant add ALIAS --keys file:PATH [--region R]...` | a test tenant by an alias (test-1) with its read-only key: a file of the owner (mode 600, `ak=` and `sk=` lines) or `pass:ENTRY`; owner side |
| `awb tenant snapshot [ALIAS]...` | list the servers, disks and elastic IPs of every region, keep the snapshot of the day and append what appeared, changed and went; GET only, owner side, fit for a daily cron line |
| `awb tenant list`, `now ALIAS`, `history ALIAS [--project CODE] [--since DATE]`, `at ALIAS DATE`, `project CODE` | what runs now, what ran when and for which project, from the snapshots; handles instead of ids, aliases instead of tenant ids |
| `awb ask serve [--port 8081]`, `awb ask QUESTION` | the Ask page: a question in plain words, answered by the model only from the knowledge base, the live price API and the tenant snapshots, with the entry of every fact; the question is name checked before it leaves; runs as its own service user that alone reads the API key; a daily budget of questions and tokens |
| `awb api write`, `awb api check` | the contract of the web API: `docs/api/openapi.yaml` is written from `awb/tcp/web/contract.py`; check refuses a file that drifted from the module, an example that does not fit its schema, an unresolved reference and an operation without `x-awb-state` (deployed or repository) |

The goal and the tags carry no name. Tags come from `rules/tags.txt` and a tag not listed there is refused. Spawn
and close each write a ledger entry. A session claims its project at start (another live session in the same project
is named), the stop hook drafts a ledger entry from the day's commits at most every two hours and keeps the English
note of the last reply. Session start says when STATE.md is over 150 lines or a project has been idle for 30
days. RESOURCES.md holds one row per cloud resource: id, type, region, cost class, expiry, state (live, deleted or
kept) and a note.

## Checks

| command | what it does |
|---|---|
| `awb check FILE...` | registered names and structured data (mail, IP, IBAN, phone...) in files; positions and classes only |
| `awb check --no-register FILE...` | structured data only |
| `awb gate PATH...` | names, secrets, private keys, tokens, home paths, blocklist words and ids in files |
| `awb gate --staged` | the same over what a commit will carry (the pre-commit hook runs this) |
| `awb gate --selftest` | prove that every class can be found, also in a commit message |
| `awb gate --message FILE` | the commit message (the commit-msg hook gives its file): comment lines and the part after the scissors line are not scanned. The Workbench runs it from `hooks/commit-msg` |
| `awb gate --install [--kb] --repo PATH` | write the commit hook of another repository: the self-test, then `--staged`; with `--kb` also `awb kb verify --staged`. It writes the commit-msg hook (`--message`) and the push hook (`--pushed`) too. `awb spawn` does it for every project |
| `awb gate --pushed --repo PATH [--remote NAME]` | every file, the author, the committer and the message of every commit a push carries and the pushed ref names (an annotated tag with its tagger and message), read from the lines git gives a pre-push hook; a secret added and removed again in the pushed history is still found; a merge commit is diffed against each parent. With `--remote` a new branch is new against that remote only. The Workbench runs it from `hooks/pre-push` |
| `awb write check FILE --mode mail` | house style: long dash, comma before and/or, banned words, "we" in a mail, commitments, references to earlier mails, vendor name, certification terms. Modes mail, doc, chat. Reported with lines, never blocking: fragments, comma splices, headings written as verdicts, connectors outside his set (and, so, then, but, also, because), a mail with too little "I" |
| `awb write keep BEFORE AFTER` | the voice pass changed no fact: numbers, identifiers, negations and code blocks of the text before and after it are compared, any difference blocks (exit 1). Prints kinds, counts and lines, never a value |
| `awb voice learn DRAFT SENT` | store a draft and the version you sent, write suggestions for the style rules |
| `awb seal check` | the checks of the seal that need no root, for the current user |

## The red-team pack (owner side)

Not an `awb` command: the case files of the red team of 2026-09-27 (`calibration/redteam/`, README.md there) run
with python from the repository root, one dimension at a time, and compare the run with the known leftovers.

```bash
.venv/bin/python calibration/redteam/harness.py calibration/redteam/structured.py --work /tmp/rt/structured --json /tmp/rt/structured.json --expected calibration/redteam/EXPECTED.md
.venv/bin/python calibration/redteam/gate_harness.py calibration/redteam/gate.py --json /tmp/rt/gate.json --expected calibration/redteam/EXPECTED.md
```

The same for forms, derived, office, pdf, mail_archive and nontext. `tests/test_redteam_pack.py` runs a sample.

## Knowledge base (`~/tcp-kb`)

| command | what it does |
|---|---|
| `awb kb find WORDS...` | entries ranked by how much of the query they carry, rare words weighing more, with grade, date and an EXPIRED flag. A service abbreviation and its plain words find each other (`rules/kb-synonyms.txt`); question words and the platform's name are not searched |
| `awb kb show KB-XXXX` | one entry |
| `awb kb scope TAG` | a briefing on one technology: facts by grade, negatives with what was tried, expired entries |
| `awb kb expired` | entries past their re-check date |
| `awb kb export --out facts.jsonl` | the facts fit to leave as a dataset, one JSON object per line (id, statement, grade, source, checked, expires, tags, tried); by default live, contract and docs only, no retired and no expired entry, every one name checked again. `--grade said` after a clearance, `--include-expired`, `--tag T`, `--scope hcs` |
| `awb kb add --scope tcp --tag T --grade G --class C --source "..." "statement"` | add one checked fact. Grades live, contract, docs, said, assumed. Classes availability (30 days), api (180), stable (365). A negative needs two `--tried "..."`. `--checked DATE` for an older check, `--force-new` over a near duplicate |
| `awb kb amend KB-XXXX "the new statement"` | correct the statement of an entry, keeping its id. Every check of `add` runs again, a negative against the tried texts the entry already carries. New ones given with `--tried "..."` (twice) replace them. `--checked DATE` when the fact itself was checked again (the date does not move by itself), `--force-new` over a near duplicate, `-` reads the statement from standard input |
| `awb kb recheck KB-XXXX --grade G --source "..."` | record a fresh check of an entry whose statement still holds: grade, source and date become those of the new check (today unless `--checked DATE`). A negative needs two `--tried "..."` of the new check |
| `awb kb retire KB-XXXX --why "..."` | withdraw an entry. The file, its history and its id stay, `find`, `scope`, `expired` and INDEX.md stop offering it, `show` still prints it with the date and the reason |
| `awb kb index` | write INDEX.md again |
| `awb kb verify [--staged] [FILE...]` | the checks of `add` over entry files; `--staged` over what a commit carries (the commit hook runs it) |
| `awb kb bench FILE` | how well `find` answers questions whose answering entries are known: first place and first five. `~/tcp-kb/bench/` holds the question sets; every refresh runs them |

`awb kb` writes files, it never runs git. The knowledge base is your own repository: create it once with
`git init` and give it an identity of its own, because the work user has no global one and a commit fails
without it.

```
cd ~/tcp-kb && git init && git config user.name "Architect Workbench" && git config user.email awb@localhost
```

Then give it the commit gate once: `awb gate --install --kb --repo ~/tcp-kb`. Every commit runs the self-test of the
gate, the gate over what it carries and `awb kb verify --staged`, so a file written around `awb kb` does not get in.

## Ledger, reports, career

| command | what it does |
|---|---|
| `awb ledger add --kind proof --done "..." --project tcp-xxxx --customer CUST-XXXX --tag T` | one activity entry. Kinds proof, inquiry, pricing, tender, training, tooling, code, other. Also `--outcome`, `--deliverable`, `--open`, `--date` |
| `awb ledger list --from 2026-09-01 --to 2026-09-30` | entries of a period |
| `awb report --by customer --from 2026-04-01` | a report by customer; also `--by tech`, `--by kind`, `--by project`, `--format html` |
| `awb career add --title "..." --role "..." --stack "..." --outcome "..." --cv-line "..."` | one career entry, no customer reference at all |
| `awb career update --since 2026-06-01` | CV bullets and a LinkedIn paragraph as a draft |
| `awb career due` | days since the last update (exit 1 after 90) |

## Review of a deliverable (inside a project)

| command | what it does |
|---|---|
| `awb review init deliverables/FILE --request REQUEST.md --tier 2 --budget 400` | the contract: request, reader, questions, budget, tier. Also `--reader`, `--mode mail`. A text for a customer or partner is tier 3; `--lowered "his words"` records that he lowered it |
| `awb review claims deliverables/FILE` | every checkable statement into `reviews/FILE/claims.tsv`; fill evidence and verdict there. `kb.tsv` next to it lists the knowledge entries per claim; one of grade live or contract that has not expired settles a claim as evidence `kb:KB-XXXX` |
| `awb review l0 deliverables/FILE` | the level-0 check: style, names, budget, claims with evidence; a computed number (a total, a sum, a saving, 20 x 8) needs `calc:K-N` as evidence |
| `awb migrate init --from azure` | start a migration in the project; `awb migrate status` and `awb migrate next` show the phases (discover, map, estimate, plan, review), the next phase file and its worker |
| `awb migrate inventory input/FILE` | the machines of a CSV or a sanitised copy into `migration/inventory.json` as R-1, R-2 ...; vCPU from the Azure size name when the column is missing |
| `awb migrate map [--region eu-de]` | each machine to the nearest TCP flavor that is not smaller, from the live price API, into `migration/mapping.tsv` with a status: ok, flag, needs-input, manual, no-fit |
| `awb migrate estimate [--term PAYG]` | the price per month of every mapped row by its OS, into `migration/estimate-TERM.tsv`, the total recorded by `awb calc` |
| `awb migrate done PHASE` | the gate of a phase; a phase whose files change later turns stale with every phase after it |
| `awb calc "20 * 8"` | exact decimal arithmetic, recorded in `calc/calc.tsv` of the project as K-N (R-005). `--let hours=730`, `--places 2`, `--label TEXT`, `--dry`; functions sum, min, max, abs, round, ceil, floor, pct. `awb calc list`, `awb calc show K-N` |
| `awb review pass deliverables/FILE` | l0 and, for tier 3, the lens results; writes `record.json` with the counts per lens, the claims by verdict, the agent runs and the open points |
| `awb review status` | valid, stale or missing record for every deliverable (the stop hook uses this) |
| `awb review calibrate` | the calibration set (`calibration/cases.json`, invented texts with one seeded defect each and five clean ones) through the script steps; lists the cases that need the review workflow |
| `awb review calibrate --lenses PROJECT` | scores a run of both workflows over `calibration/workflow-cases.json` in a lab project: per case the lenses with open blocking findings (an EXTRA finding counts for the extra-content cases), caught, missed or false alarm |

The saved workflow `workflows/review.js` runs the blind checks of tier 2 and 3 first and then the lenses, which see
the blind results; a lens finding without a quote or evidence is dropped and counted, a refute agent that cannot
settle a finding leaves it plausible (soften the sentence, set `softened`). After every fix and the cut to the
budget, `workflows/request-check.js` is the last check (tier 3): where each question is answered, what nobody asked
for, what is stated more firmly than its grade. Both workflows are started by him or on his word.

A text in his name follows the drafting skill (`seal/work-claude/skills/drafting/SKILL.md`, installed by the seal
for the work user): facts first (the review above), then the checked version is kept as
`reviews/FILE/before-voice.md`, then the voice pass, `awb write keep`, `awb write check`, the cut to the budget as
a last step and both checks again. After he sends it, `awb voice learn DRAFT SENT`.

## Used by the client, not by hand

`awb hook prompt | pre-write | post-write | stop | session-start` are the hooks of working sessions (a hook that runs
over 50 seconds stops itself and fails closed). While the vault is locked, or the name check cannot run for any
other reason, every prompt of a work session is refused with the time of the lock and the unlock to run, and a
session starts without its project files. `awb vault status`, `awb board` and the portal's `/health` open with
`locked since <time>`. `awb vault serve` is the vault daemon under systemd.

On a sealed host `/usr/local/bin/awb` is a root owned wrapper that runs `/opt/tcp-awb/venv/bin/python3 -I -m awb`,
so a package in the invoking user's site folder or on `PYTHONPATH` is never loaded ahead of the installed code. A
project gets the hooks of that installed command only: `awb spawn` from the repository environment refuses on a
sealed host (fail closed); run it as the work user.
