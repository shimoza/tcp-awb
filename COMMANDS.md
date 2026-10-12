# Commands

`awb` is the one command of the Architect Workbench. Exit codes: 0 ok, 1 findings or a file withheld, 2 usage or error.
`awb <command> --help` shows the options of any command.

Two sides. The owner side holds the register of names and the originals and is run by you in your own shell. The
work side is what Workbench sessions (and you) run inside `tcp-` projects. A name is only ever typed on the owner
side.

## Owner side (your own shell, never through a session)

| command | what it does |
|---|---|
| `awb init` | create `~/tcp-shared`, `~/tcp-vault` and an empty register (done once) |
| `awb import tcp-xxxx [FILE...]` | the one door for customer material: takes the FILE arguments, else every new file of the project's `in/` folder in the bucket, else everything in `~/tcp-vault/inbox`, and runs the intake for the project's customer in wipe mode. The copies go to `~/tcp-shared/outbox/CUST-XXXX/` (for a project without a customer to `~/tcp-shared/outbox/tcp-xxxx/`, with no customer code) and carry the customer as its code and every other name the rules recognise as a token (`[person 1]`, `[company 1]`, `[place 1]`, `[name 1]`, numbered within the import); nothing stops, no editor opens. It prints the files taken, the copies written, the values wiped per class, the pictures held and the files withheld, never a name, and the sessions of the project read "new input" at their next prompt. Your own terminal only |
| `awb import tcp-xxxx --customer CUST-XXXX` or `--customer new` | only when a project without a customer is to get one (without it such a project imports with no customer code): `new` issues `CUST-` and the project's four characters (a random code when that one is taken). A customer without a registered form is asked for its written forms on the terminal, one per line, an empty line ends; for a project without a customer an empty first line issues the code without a form |
| `awb import tcp-xxxx --redo` | the repair of a wiped term: after `awb register keep "the term"`, the originals of the customer's last import run again from the vault and the copies replace the earlier ones under the same file ids; the session reads "replaced" |
| `awb import tcp-xxxx --review` | the rare case where a person, a partner or a place must keep a code: the run stops once, an editor opens with the candidates (`p` person, `o` company, `c` this customer, `s` place, `+` one more form, `-` later, no mark: keep), then it goes on in wipe mode and a candidate left for later is a token |
| `awb vault show` | the newest private report (what was wiped, by class and rule, and the forms the run learned) of `--customer CUST-XXXX` or of the only customer with a report of the last day; a FILE shows that file. Your own terminal only, never to a pipe or inside an assistant session |
| `awb register keep "phrase"` | a term that was wiped by mistake is never wiped again, in any customer's material (then `awb import --redo`) |
| `awb register keep --list` | the kept phrases (vault side only) |
| `awb register add CUST-XXXX CUST "full company name"` | register one written form of a code; add every short form too. `-` reads the form from standard input; `awb import` asks for the forms of a new customer itself |
| `awb register new CUST` or `PART` | print a fresh code; `PERS`, `ORG` and `SITE` codes come from `--review` only |
| `awb register list` | codes, kinds and counts of forms, no names |
| `awb register list --forms` | the same with the written forms (vault side only) |
| `awb register retire CUST-XXXX` | retire every form of a code; the code is never reused |
| `awb words update` | rebuild `rules/known-words.txt` and `rules/known-phrases.txt` (the capitalised words and two-word phrases of the public mirrors, which the intake reads as terms) after `awb mirror docs` or a new service description |
| `awb images list [CUST-XXXX]` | the pictures the intake held on the vault side (office media, pdf images, image files), per file: held, released, left out |
| `awb images release CUST-XXXX FILEID N...` | after you looked at them: copy the chosen pictures to `~/tcp-shared/outbox/CUST-XXXX/images/`; `--all` for every held picture of the file |
| `awb reveal FILE --out PATH` | put the real names back into a finished text: every code becomes its first active form; the named file gets mode 600 and never goes where a session reads |
| `awb vault status` | state of the vault: plain, locked or unlocked |
| `awb vault encrypt` | encrypt the vault once (new passphrase twice), then unlock |
| `awb vault unlock` | give the passphrase to the vault daemon, after every reboot |
| `awb vault lock` | drop the passphrase and the register from the daemon's memory |
| `awb vault reload` | hand the vault daemon to a new process of the installed code without a lock (what `awb deploy` and `systemctl reload awb-vaultd.service` run): `vault <state>, pid <n>, release <commit>`. While it runs, a write to the vault (unlock, lock, a register save, a seal) answers `busy`: the vault daemon is reloading, try again. A stop, a crash or a reboot still lock the vault |

Every import first runs its self-test over the planted cases of `awb/planted.py` and takes nothing in when one of
them is no longer found, when a planted name of the wipe cases comes out unwiped, when a control term is wiped or
when the customer inside a company shape does not become the code. Every output passes the final check: the matcher,
the structured patterns and the candidate rules over its body, and a second check by another method (the letters of
whole words against every registered form and every form the run learned). A file that fails it is withheld and its
original stays in the inbox. What the rules do not recognise passes: a first name alone in prose, a name only glued
into an identifier, a name in a picture (the pictures stay held, `awb images`).

The pieces behind `awb import`, one line each:

| command | what it does |
|---|---|
| `awb intake --customer CUST-XXXX [--review] [FILE...]` | the intake alone, in wipe mode (no FILE: the vault inbox; `--customer new` issues a random code) |
| `awb bucket pull CODE` | new files of `in/` into the vault inbox, then the intake |
| `awb register review CUST-XXXX` | the review of the newest stop of `--review` by hand |

## The bucket (owner side)

One folder per project in the bucket `awb`: `<YYYY-MM>/<project-code>/in/` for your originals and `out/` for
results. A folder is looked for under every month and used where it is; a new one goes under the month the
project was created. A drifted folder is renamed in the bucket, never the project.

| command | what it does |
|---|---|
| `awb bucket sync` | create the folders of every active project; report folders that match no project (count per month, no name) |
| `awb bucket folder CODE` | the folder of one project, created when missing |
| `awb bucket pull CODE` | new files of `in/` into the vault inbox, then the intake for the project's customer (for a project without one under the project code), in wipe mode; it prints the values wiped per class. A file is taken once (`--again` takes it again). `awb import CODE` does the same and more. Your own shell |
| `awb bucket put CODE FILE` | FILE into `out/`; `--replace` overwrites, `--reveal` puts the real names back first (your own shell). The send gate runs first: for a project with a customer or partner it refuses a deliverable without a valid review at tier 3 (or a tier he lowered) and a tier 3 review without the request check; an internal file leaves with a warning |
| `awb dataset put [--date D] [--replace] [--github]` | the zip, the how-to PDF and the manifest of the newest built dataset (or the one of `--date`) into `datasets/tcp-facts/<date>/` of the bucket and the date into `datasets/tcp-facts/LATEST`. `--github` also copies facts.md, facts.jsonl, services.md, topics/ and prices/ into the root of github.com/shimoza/tcp-facts through a clone under `~/tcp-shared/datasets/tcp-facts.git` (made on first use with gh), commits them as the GitHub account, tags the date and makes a release of it with the zip, HOW-TO.pdf and MANIFEST.json, the counts as notes; the owner's commit identity, an old author setting or a registered name in any file refuses it, and a date already tagged needs `--replace`. Your own shell, it takes the bucket key and your gh login |
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
| `awb deploy status` | the journal, the release each daemon runs, locked or down since when, a split between a daemon and the code, the egress filter's mode and its counts of the last 24 hours |
| `sudo awb deploy --only awb-vaultd.service awb-keyd.service` | these units only, also when nothing changed (after a library upgrade) |
| `sudo awb deploy --all` | restart every installed unit |
| `sudo awb deploy --rollback` | the release that was live before this one |
| `sudo awb deploy --to COMMIT` | another commit of the repository |
| `sudo awb deploy --task ID` | the green commit of a build task: fetches `main` of `/srv/awb-build/main.git`, fast-forwards your `main` to it (with `--dry-run` it fetches only) and deploys the commit `runs/ID/<n>/commit` names. Refused while your tree has changes, your copy is not on `main` or your `main` has commits `main.git` lacks, for an id without a run, a run that is not `done` and a commit that is not on `main`; a task that is live already is refused too. Excludes `--to` and `--rollback` |

The tree must be clean (commit first). Two refusals hold for every deploy, with the line `git pull --rebase
/srv/awb-build/main.git main`: a deployed commit (the journal and the release `src` points at) that your repository
does not know, and a target that does not descend from it unless `--to` or `--rollback` names it. Without them a
deploy from a copy that is behind would take back the build runner's work. The deploy runs the installed copy of its own code, so a change of
`awb/deploy.py` is live from the following deploy. `seal/setup.sh` (full) stays for the first seal of a host.

## The build queue (owner side, presentations/self-build)

A build task is a block in `build/queue/QUEUE.md` of your working copy (untracked) and a prompt file beside it.
The runner (`awb build serve`, step SB3) reads the queue and never writes it; its records are
`/srv/awb-build/runs/<ID>/<n>/`, owner 700: `task.md` and `prompt.txt` (the snapshot at the start), `result.json`,
`session`, `deadline`, `ended`, `task.bundle`, `status.txt`, `checks.log` (one line per check), `suite.log`,
`result.md` (fixed fields), `message.txt` (the agent's text, for `awb build show` alone), `commit`, `mail`, `state`.

| command | what it does |
|---|---|
| `awb build add FILE` | a task into the queue. FILE holds the block (`## B-012: title`, then `key: value` lines; `model`, `effort`, `budget` and `clock` may be left out, `prompt` and `end` are written for you), a line `---` and the prompt. The grammar and the gate's classes with the register run first (a refusal names the key or the class and the line, never the value); then `build/queue/B-012.md` and `QUEUE.md` are each written to a fresh name and renamed, so a reader sees the old queue or the new one. Refused when the id is in the queue or has a run, and when the name check cannot run |
| `awb build list` | one line per task in queue order: id, state, since when, what it waits on (the runner, a dependency, paths another open task holds, your answer, your word); a block that is refused or not complete yet gets its own line with the key and the line number |
| `awb build status [--ended]` | the board line (`build: B-012 done 14:02, B-013 running since 14:10, ...; live eb928d2, main 1a2b3c4, 1 not deployed`, the last part read fresh from `/opt/tcp-awb/DEPLOYED` and `main.git`); with `--ended` the one line of each run that ended since the last call, each once |
| `awb build show ID` | the agent's last message of the newest run (`message.txt`), control characters dropped; to your own terminal only, refused in a pipe, a file or an assistant session |
| `awb build answer ID TEXT` | the answer to a task that is `asked` (gate classes with the register first); the runner resumes the same session |
| `awb build retry ID` | a new numbered run of a task that ended without `done` (hung, red, stopped, held, interrupted, stale, asked); it starts from `main` |
| `awb build widen ID PATH...` | more paths for a `red` task; the runner checks the kept branch again, no new session |
| `awb build stop [ID]` | without ID: the runner ends after the current task; with ID: the runner stops `awb-task-ID.service` of that running task |
| `awb build serve` | the runner of the build queue, the `ExecStart` of `awb-buildd.service`, as the owner: one runner (a lock on `runs/.runner.lock`, a second one exits 1), the reconcile of a run left running, then every 30 s the open business (a green run's push, a held push after ten minutes, the checks again after `widen` or once the name check runs) and the next task. Before a session it needs the vault unlocked, the key service loaded, the gate's self-test, 5 GB free, the owner's identity, the model key (only with `build_model = key`) and a no-op unit as `awb-build`; while one fails the queue is held (one board line, one mail). With `build_model = login` (the default) a task whose build user has no login ends at once as `held: login missing`, mailed once with the one command you run, and holds the queue until `awb build retry ID`. Exit 0 after `awb build stop`, 75 after `restart-wanted` (the unit starts again with the new code). A deploy never starts the runner and defers its restart while a build task runs |
| `awb build selftest` | the runner's planted cases (THREAT-MODEL N6, N7, N9 to N12 and a push that is not a fast-forward) in a throw-away folder; each must be refused or classified, exit 1 names the case that passed or a case that is missing from its fixed list. `seal/verify.sh` and `awb seal check` run it |
| `awb build probe` | as the owner: the network of a build task, proven in units of the runner's own `sudo -n systemd-run` form with the address list, the hidden places and the environment of a task's session. A control unit shows the build user first; then the model endpoint and, with `build_model = login`, `platform.claude.com` connect, a TCP API host and `github.com` do not. One PASS or FAIL line each, exit 1 on a FAIL; the record `tcp-shared/build/probe.tsv` is what `awb seal check` reads. `seal/verify.sh` runs it |

`answer`, `retry`, `widen` and `stop ID` refuse an id that never ran and a task that is running or waits for the
runner. States: `queued`, `running`, `green`, `done`, `asked`, `answered`, `widened`, `red: <reason>`, `hung`,
`hung: suite`, `stopped: <subtype>`, `held: <reason>`, `interrupted`, `stale`.

## The egress filter of the work user (seal/egress/ALLOW.md)

| command | what it does |
|---|---|
| `awb egress status [--json]` | any user: the mode, when the table was loaded and refreshed, and per class what the last 24 hours counted outside the list (`allowed` counts the connections to the list) |
| `sudo awb egress load` | root: resolve the host names of `seal/egress/ALLOW.md`, load the table `inet awb_egress` in the mode of `/etc/awb/paths.conf` or refresh its address sets in place, record the counters. `seal/setup.sh`, `awb-egress.service` at boot and `awb-egress.timer` every 5 minutes run it |
| `sudo awb egress enforce [--accept CLASS...]` | your own shell: refuse every new connection of the work user outside the list. Refused (exit 1, the mode stays count) unless the last 24 hours were counted without a gap of more than 30 minutes and no class counted a connection but `probe` (the planted one of `seal/verify.sh`) and the classes you name with `--accept` because you expect to lose them |
| `sudo awb egress count` | your own shell: back to counting, nothing refused; the 24 hours start again |

The mode is the line `egress = count|enforce` of `/etc/awb/paths.conf` (count when it is missing); the setup writes
count and keeps what it finds. The classes: `probe`, `metadata`, `dns`, `ntp`, `tcp_api`, `github`, `ssh`, `mail`,
`web`, `other_tcp`, `other_udp`, `icmp`, `other` (`seal/egress/ALLOW.md` says what each holds).

## The console page (owner side, your own shell)

| Command | What it does |
|---|---|
| `sudo awb web publish --from-queue ID` | publish the page of a UI queue run: the applied source of a completed run or the candidate of a `--candidate-only` run in state ready, only with a validation that starts with PASS. Checks names, secrets, tokens, private keys, one `<title>` and no external script, keeps the current page as `index.html.before-ID`, installs the new one 644 root:root, prints the size and the backup |
| `sudo awb web publish --from-queue ID --newest` | the same, but only for the newest PASS run among the completed and the ready runs, with an id after the last published one and bytes other than the live page; keeps the newest 10 backups. The owner host's Publish runs exactly this through a root unit |
| `sudo awb web publish FILE` | the same for a file; the backup is `index.html.before-<date-time>` |
| `awb web publish --dry-run --from-queue ID` | the checks only, without sudo; nothing is installed |
| `sudo awb web publish --rollback [NAME]` | put a backup back (the newest without NAME); the replaced page is kept as a backup too |
| `awb web status` | the size and time of the page, the last backup, the gateway (its status socket) and `/health`; under sudo also the sign-ins of the last 24 hours by result, a closed site and every login that waits |
| `sudo awb web user add LOGIN` | a console login: a random password of 20 characters printed once, only its salt and PBKDF2 digest kept in `/etc/awb-web/users.json` (root:awb-web 640). The first login ends the single login of `auth.json`. Refused inside an assistant session |
| `sudo awb web user add LOGIN --level owner` | an owner entry: the password and a TOTP secret for the owner host, printed once as a key, an otpauth line and (with qrencode on the host) a QR code for the authenticator app; on the owner host an owner entry is never locked: each failure delays the next try of the name from that address up to 60 seconds, a wrong code after the right password the name from every address; on the main host it waits after 5 failures like any login |
| `sudo awb web user reset LOGIN` | a new password for the login (and a new TOTP secret for an owner entry), printed once after a first line that says the old ones no longer work; its generation grows and its sessions end |
| `sudo awb web user remove LOGIN` | the login goes and its sessions end; without logins nobody signs in |
| `awb web user list` | the logins with their level, never a digest |
| `awb owner status [--write]` | the owner level's status: the vault and the key service, the release, the page, the UI runs and the intake counts per customer code, codes and states only; `--write` checks every field and the names and writes `/var/lib/awb-owner-status/status.json` for owner-actions (its timer runs it every minute as you) |
| `sudo awb web host HOST` | the host name of the owner level in `/etc/awb/paths.conf` (`owner_host`), one level under the zone of the site and not the site itself; once, before the deploy that brings the owner level (the web side's installer stops without it) |

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

## The price workbook

`awb pricebook` builds one workbook per estimate from a specification file with live prices and compares versions. It
needs no key and no vault, so sessions run it. Runtime code is the standard library only.

| command | what it does |
|---|---|
| `awb pricebook build SPEC [--out FILE] [--lang en\|de] [--against OLD] [--no-record] [--project DIR]` | refuses a service the service description does not offer (before any price call), asks the public price API once per region, prices every line from the record it found and writes `FILE.xlsx` (default: the specification with the ending `.xlsx`) and a Markdown summary `FILE.md` for a mail. Nothing is written unless every line is priced. `--lang` picks the headers, the fixed texts and the number format of the summary (default: `language:` of the specification, else en). `--against OLD` (a workbook or a specification) adds the sheet of changes. The two totals are recorded with `awb calc` when the specification lies in a project, so the review takes them with `calc:K-N` (`--no-record` leaves that out). Exit 0 done, 1 a line refused (nothing written), 2 the price API did not answer, a bad specification or an error |
| `awb pricebook diff OLD NEW [--lang en\|de] [--no-prices]` | what changed between two specifications, two workbooks or one of each, line by line (matched by item name) and in the totals. A specification is priced now, both versions against the same snapshot; `--no-prices` compares the settings of the lines only. Exit 0 no difference, 1 a difference, 2 the price API did not answer or an error |

A specification is a text file (or JSON) that a person can read. Settings at the top (`title`, `region`,
`language`), then one block per line item, headed by its name in square brackets. A line starting with `#` is a
comment and a line starting with a blank continues the key above.

```
title: Sizing for CUST-Q7M4
region: eu-de
language: en

[web servers]
service: ecs
flavor: s3.large.2
os: Open Linux
quantity: 4
mode: payg
assumption: Two zones and all day.
assumption.de: Zwei Zonen und rund um die Uhr.

[data disks]
service: evs
flavor: vss.ssd
quantity: 2000
assumption: 500 GB per server.
```

| key | meaning |
|---|---|
| `service` | the service as the service description or the price API names it (`ecs`, `Elastic Cloud Server`); it is checked against the service description |
| `api` | the price API service when it is not the same (`ecsflex` under ECS); needed when `service` is not written like an API name. A name in `rules/services-not-offered.txt` or a withdrawn section is refused here too |
| `flavor`, `os` | the record: `os` is the OS unit (`Open Linux`) or a part of it that fits one record only (`Linux` alone fits four of them); it is needed when the flavor has several OS tiers |
| `id` | the id of a price record, instead of `flavor` and `os` (for records without a flavor and for tiered rates) |
| `region` | `eu-de`, `eu-nl` or `eu-ch2`; the setting at the top is the default, which is `eu-de` |
| `quantity` | a positive number with a decimal point, not a comma |
| `hours` | hours per month, for an hourly rate only; 720 when left out (the public calculator counts 720). Not allowed for a reserved rate or a monthly unit. At most 744 |
| `mode` | `payg` (default), `r12`, `r24`, `r36` (reserved) or `ru12`, `ru24`, `ru36` (reserved, up front); also written `pay as you go`, `reserved 12`, `up front 24` |
| `assumption` | free text per line; `assumption.de` and `assumption.en` replace it in a workbook of that language |

A line needs a unique item name. The JSON form has the same keys in a list `lines` (each with a `label`), with
`assumption_de` for `assumption.de`. At most 120 lines. A specification carries codes (`CUST-Q7M4`), never a name.

The monthly figure of a line is the rate times quantity times hours rounded half up to the cent (a reserved rate is a
flat monthly value, so rate times quantity); the yearly figure is the monthly figure times 12. The workbook has the
sheets Lines, Sources and (with `--against`) Changes, in German Positionen, Quellen and Änderungen. The lines carry
the unit price, quantity, hours, monthly and yearly figure (formulas with their stored results), the assumption in
its own column and the id of the price record. Below them a block of totals holds the monthly and yearly total and the
cells `Months per year` and `Hours per month` that the formulas use. Sources holds the time of the price API
snapshot of each region, the revision of the service description, the section of each service, notes such as a
tiered rate and the recorded calculation ids. A rate in a currency other than EUR is refused.

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
| `awb refresh` | one run of the refresh with a report in `~/tcp-shared/refresh/<date>/REPORT.md`: mirrors brought up to date, a price snapshot and every value changed since the one before, the expired knowledge entries with how each can be checked again (`worklist.jsonl`) and the retrieval numbers, the red-team pack of wipe mode through the installed intake (its selfcheck first, the leaks and losses per case module also in `refresh/wipe.tsv`, overdue when they grew against the last line), projects whose STATE.md is older than 14 days and stale pointers (a command the rules name that does not exist, a knowledge id the code cites that is retired, a contract entry citing an old revision), and at the end the dataset of the day (`awb dataset build`), not built when a part before ended in an error. `--part P` for some parts, `--no-update` to read the mirrors only. Exit 1 when something is overdue |
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
| `awb spawn query --goal "..." [--customer CUST-XXXX] --tag iam` | a query `tcp-<code>` with its files, hooks and git: the knowledge base, the docs mirror, live prices and calculations, no test tenant (the key service refuses it). It ends with the project's folder `<YYYY-MM>/tcp-<code>/` with `in/` and `out/` in the owner bucket, made by the key service; when the service cannot be reached it says so in one line and goes on (the first take or `awb bucket folder CODE` makes it). The console's creation runs the same spawn |
| `awb spawn project --goal "..." [--customer CUST-XXXX] --tag vpn` | a project: everything a query has plus the test tenants through the key service |
| `awb spawn query --goal "..." --customer CUST-XXXX --from-outbox` | the sanitised copies waiting in `~/tcp-shared/outbox/CUST-XXXX/` move into `input/` and the first commit |
| `awb projects kind tcp-xxxx project` | switch a query to a project (or back); a ledger line records it. The old kinds engagement, topic and code read as query, lab as project. Run by a session it changes the project's row only; run by you in your own terminal it also writes your grant (`<vault>/grants.json`) with the customer of the row at that moment, and the key service lets a session reach a test tenant only with that grant and an active row of the kind project. The key service takes a granted project's customer from the grant alone (owner-inbox take, project take, send gate of a put, lease) and refuses a row that names another one. A project you never granted has no customer for the key service: its owner-inbox take holds, its project take runs under the project code and its put meets the strict send gate. Run it once more for a project granted before 2026-10-10 so its grant records the customer; a change of the grant's customer is printed |
| `awb projects list` | the project register with the goal of each project (checked for names again) and the session that holds each active project; `--paths` shows the folders instead of the goals |
| `awb projects delete CODE...` | remove the folder of closed projects (`awb close` first); refused while RESOURCES.md lists a live resource or a session holds the project; the code stays registered as deleted and `awb projects list` hides it (`--all` shows it) |
| `awb projects open tcp-xxxx` | owner side: start a session in the project as the work user in this terminal (`sudo -n -u` the work user, then `claude` in the folder the register names); refused for an unknown or deleted code, as the work user and where `sudo -n` is not allowed |
| `awb projects check` | tcp- folders that no project registered (a name that is not a code shows as "a folder"); run by you, also every granted project whose grant records no customer while its row names one (with the `awb projects kind` line to run) and every row whose customer differs from its grant, by code only |
| `awb projects sync [CODE...]` | bring the template lines of CLAUDE.md and STATE.md of every active project (or the ones named) up to the current rules: an old line is replaced, a line the template gained is added, every other line and the project's own notes stay; nothing is committed. Running sessions hear of changed rules by themselves: the prompt hook tells a session once when the rules it loaded at its start changed |
| `awb board write [--out DIR]`, `awb board show` | the status of every active project for management, on demand: the Status: and Next: lines of STATE.md with their date and time, the commits newer than them (marked, never written), what each project waits on, its first open items, its deliverables by review state and its live resources, the line of the last wipe measurement (`wipe: L leaks, M losses of N (date)`) and the build line from `<shared>/build/state.tsv` (`build: B-012 done 14:02, B-013 running since 14:10; live eb928d2, main 1a2b3c4, 1 not deployed`, ids, fixed state words, short hashes and times only; `build: no runner state yet` without the file). `write` puts `board-<date>-<time>.md` and `.html` (prints on A4) into `presentations/board/`, `show` prints the Markdown; codes only. The portal shows the same live under Board (`GET /api/board`) |
| `awb close tcp-xxxx` | close a project: refused while OPEN.md holds items or RESOURCES.md lists a resource that is not deleted or kept; `--force` closes anyway and says what is left |
| `awb english list [--month YYYY-MM]` | the English notes the sessions wrote, kept after a name check; once a month, the three that keep coming back |
| `awb portal serve [--port 8080]` | a read-only web page over the knowledge, live prices, the projects and the reviews, on 127.0.0.1 only; reached through a tunnel with an access check in front, never directly |
| `awb keys unlock` | load the keys and secrets of every tenant from your password store into the key service (`awb/tenant/<alias>/ak`, `sk`, `lab/ak`, `lab/sk`, `secret/<name>`); after every start of the service. `awb keys lock` forgets them, `awb keys status` shows aliases, roles and secret names, never a value. `awb keys reload` hands the service to a new process of the installed code and keeps the keys (what `awb deploy` and `systemctl reload awb-keyd.service` run). Your own shell |
| `awb cloud call METHOD SERVICE PATH --tenant ALIAS [--role read\|lab] [--region R] [--query K=V] [--body FILE] [--project CODE]` | one call through the key service, for sessions too: the service signs, a write needs the lab key and an active project, a body may fill a password field with `{{secret:NAME}}`, the answer comes back with every key and secret taken out. `awb cloud tenants` lists the tenants |
| `awb cloud lease-check ALIAS [--region R] [--yes] [--no-terraform]` | the live check of a leased key (T12 part 3, P2 and P3), owner side: the key service mints a 15-minute temporary key of the lab key (op lease: the Terraform services, OBS only on buckets named after a project, IAM and both exchange buckets denied; never from the lab key of the bucket tenant of keys.conf, so the check runs on another tenant), then one read and one throw-away create and delete per service (VPC and subnet, ECS server group, EVS disk, DNS private zone, shared ELB, NAT gateway; IMS read only, OBS not checked) and one Terraform plan, apply and destroy of a VPC with the key in its environment only; everything made is deleted newest first and checked gone; without `--yes` a dry run |
| `awb lab list`, `awb lab dry-run RECIPE [--param K=V]...` | the lab recipes of `recipes/` with their question, their cost guard and how many of their API shapes are still to confirm live; the dry run prints every call a run would make with its checks and its teardown; it makes no call |
| `awb lab run RECIPE --project CODE --tenant ALIAS [--param K=V]...` | a live check on a test tenant in one command, from a project of the kind project: the project's row and grant first, then one read through the key service for its grant; every resource tagged `awb-project` and `awb-expiry` (in the body, a tag call or its name) and a row in RESOURCES.md the moment it exists; the checks; then the teardown, which always runs (also on a failed check, an error, the time budget and Ctrl-C or SIGTERM) and proves each resource gone with a read. A resource it cannot delete keeps its row as live with the line to delete it. The evidence goes to `evidence/lab-<recipe>-<date>-<n>.md` (every call with its status and the fields read, the checks, the teardown proof, the time and the resource count; ids as handles, no key, token or secret) and ends with a ready `awb kb add` line with the grade live when every check passed. A parameter outside its pattern and a recipe over its guard (at most 12 resources for at most 120 minutes) are refused before any call. Exit 0 passed, 1 a check failed, 2 refused or stopped, 4 a resource left live. The format: `recipes/README.md` |
| `awb inbox take WORDS...` | a session takes the file you described, from either inbox: your words as they are ("the pdf", "эксель", "the newest", a part of the name; `|` between several ways of saying it) are matched inside the key service, so no exact name is needed: a kind and "the newest" meet every file, the same letters or a part of the name only a file of the lab inbox whose name is shown; a file of the owner inbox or of `in/` is never found by a word of its name. One file found: from the lab inbox (`<lab bucket>/inbox/`) after the name check into `input/` and the project's `in/`, from the owner inbox (`awb/inbox/`) only through your intake in wipe mode, the session gets the sanitised copies. The new files of the project's own folder `in/` of the owner bucket are listed beside the inboxes for every project and taken the same way, for the project's customer or, without one, under the project code; "take the files from in" takes all of them. A project without a customer takes nothing from the owner inbox, nor does a project you never granted (`awb projects kind CODE query` once). A file of the owner inbox above `max_mb` or in a folder that is not exactly one `CUST-XXXX` stays there. A registered name in a lab inbox file holds it, and a file of the owner inbox that cannot be read as text is held with a mail that says `held: run awb import tcp-xxxx as the owner in your own terminal`. None or several: both inboxes are listed (id, kind, size, time; names of the owner inbox are never shown) and the session takes the one you mean with `--id ID`. `--all` takes every file of both inboxes. Every refusal is the one line `held: run awb import tcp-xxxx as the owner in your own terminal`. `awb inbox list` shows both inboxes |
| `awb xchg put FILE [--as NAME] [--image --reason TEXT]` | a result into `<lab bucket>/<code>/from-session/<date>/<file id>` after the name check of its content and of its name (the `--as` name or its own, also for `--image`) and, for a customer project, the send gate (the key service runs both again, the send gate strict unless your grant records no customer); you get a mail with the code, the id and the size, never the name. `awb xchg list` |
| `awb paste [--as NAME]` | a long terminal output into the project instead of the chat: the text on standard input passes the name check and the secret check, addresses, web addresses, phone numbers and MAC addresses are masked (`[ip]`, `[url]`, `[phone]`, `[mac]`) and it lands in `notes/pastes/<date>-<n>.txt` (`<date>-<NAME>.txt`); a registered name, a mail, bank data or a secret refuses it and nothing is written. The session reads the file |
| `~/.config/awb/keys.conf` | your settings of the exchange, sent with `awb keys unlock`: `bucket_tenant` (the alias whose lab key reaches both buckets), `lab_bucket`, `owner_bucket`, `region`, `max_mb` (default 100), `notify_topic` (an SMN topic urn for the mails), `tf_services` (the services a leased key may use, comma-separated, default `ecs, evs, vpc, elb, nat, dns, obs, ims`; a shorter list narrows every lease, an empty line allows none, `iam` or a name that is no service stops the unlock) |
| `awb tenant setup ALIAS --domain-id ID [--region R] [--lab-key] [--alerts [--topic URN]]` | a new test tenant in one command, from the admin login you put into pass once (`awb/admin/ALIAS/domain`, `user`, `password`): group awb-read with Tenant Guest on all projects, user awb-read-<number> with its key into `awb/tenant/ALIAS/ak\|sk` (through stdin), with `--lab-key` a key of the admin user into `lab/ak\|sk`, with `--lab-user` in its place the user awb-lab-<number> in group awb-lab with the Terraform services' system policies on the region's project only (OBS on the account, no IAM) and its key into `lab/ak\|sk` (a lab key of the admin there is replaced and stays in IAM until you delete it), with `--alerts` the CTS alert awb_new_access_key to the topic of keys.conf, then `tenant add`, `awb keys unlock` and the first snapshot; makes only what is missing, prints names and counts, never a value; owner side |
| `awb tenant add ALIAS --keys file:PATH [--region R]...` | a test tenant by an alias (test-1) with its read-only key: a file of the owner (mode 600, `ak=` and `sk=` lines) or `pass:ENTRY`; owner side |
| `awb tenant snapshot [ALIAS]...` | list the servers, disks and elastic IPs of every region, keep the snapshot of the day and append what appeared, changed and went; GET only, owner side, fit for a daily cron line |
| `awb tenant list`, `now ALIAS`, `history ALIAS [--project CODE] [--since DATE]`, `at ALIAS DATE`, `project CODE` | what runs now, what ran when and for which project, from the snapshots; handles instead of ids, aliases instead of tenant ids |
| `awb ask serve [--port 8081]`, `awb ask QUESTION` | the Ask page: a question in plain words, answered by the model only from the knowledge base, the live price API and the tenant snapshots, with the entry of every fact; the question is name checked before it leaves; runs as its own service user that alone reads the API key; a daily budget of questions and tokens |
| `awb api write`, `awb api check` | the contract of the web API: `docs/api/openapi.yaml` is written from `awb/tcp/web/contract.py`; check refuses a file that drifted from the module, an example that does not fit its schema, an unresolved reference and an operation without `x-awb-state` (deployed or repository) |

The goal and the tags carry no name. Tags come from `rules/tags.txt` and a tag not listed there is refused. Spawn
and close each write a ledger entry. A session claims its project at start (another live session in the same project
is named), the stop hook drafts a ledger entry from the day's commits at most every two hours and keeps the English
note of the last reply. It also holds a reply that shows him a draft (a file under deliverables/ or a mail draft)
before `awb write check` of that file ran clean in the same turn, with one line naming the file; a mail written
into the reply must go into a file first, and a draft in Russian is exempt. Session start says when STATE.md is over 150 lines or a project has been idle for 30
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
| `awb gate --message FILE` | the commit message (the commit-msg hook gives its file): comment lines and the part after the scissors line are not scanned. After the register check a name-shaped word the register does not know refuses as `candidate-person`, `candidate-company` or `candidate-unknown` with its line: reword it, or put a technical term into the section intake-seed of `rules/allowed-terms.txt`. The Workbench runs it from `hooks/commit-msg` |
| `awb gate --install [--kb] --repo PATH` | write the commit hook of another repository: the self-test, then `--staged`; with `--kb` also `awb kb verify --staged`. It writes the commit-msg hook (`--message`) and the push hook (`--pushed`) too. `awb spawn` does it for every project |
| `awb gate --pushed --repo PATH [--remote NAME]` | every file, the author, the committer and the message of every commit a push carries and the pushed ref names (an annotated tag with its tagger and message), read from the lines git gives a pre-push hook; a secret added and removed again in the pushed history is still found; a merge commit is diffed against each parent. With `--remote` a new branch is new against that remote only. The Workbench runs it from `hooks/pre-push` |
| `awb write check FILE --mode mail` | house style: long dash, comma before and/or, banned words and jargon, "we" (also wir and unser) in a mail or a document, commitments, references to earlier mails, vendor name, certification terms, a paragraph with a bold lead, an invented name for an option, admissions of weakness, sales tone, the history of the draft. Modes mail, doc, chat (chat reports the drafting tells without blocking). Reported with lines, never blocking: fragments, comma splices, headings written as verdicts, connectors outside his set (and, so, then, but, also, because), a mail with too little "I", sections the request did not ask for (`--request FILE` names them by heading) |
| `awb write translate FILE --to de --to ru` | the German version and his Russian reading copy from the accepted text in one step: FILE.de.md and FILE.ru.md next to it, every number, identifier and code block kept (refused on a difference), the German text through `awb write check` (the Russian copy is exempt). The model is reached through the Ask path only; where its key is not readable (a working session) the prompt file goes to `reviews/.translate/` and the command stops with one line; translate along it and run it again with `--verify` |
| `awb write keep BEFORE AFTER` | the voice pass changed no fact: numbers, identifiers, negations and code blocks of the text before and after it are compared, any difference blocks (exit 1). Prints kinds, counts and lines, never a value |
| `awb voice learn DRAFT SENT` | store a draft and the version you sent, write suggestions for the style rules |
| `awb seal check` | the checks of the seal that need no root, for the current user; on a host with the egress filter also its load and mode, and its counts as comment lines |

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
| `awb dataset build [--out DIR] [--date D] [--force] [--no-live] [--price-sample N]` | the dataset TCP Facts, checked while it is built: a fact that names a service the service description does not offer without saying so stays out and is named (the check proved first on a planted BMS fact), and N rows per region (default 3) are fetched again from the live price API and compared by id and price after a planted wrong price proved the comparison; a mismatch refuses the build. Its price lists without the services the service description does not offer (withdrawn sections and `rules/services-not-offered.txt`, named in the CSV header; the files and the PDF metadata credit `TCP Facts, github.com/shimoza/tcp-facts`, never a person, and no setting changes that) as a folder `tcp-facts-<date>/` and its zip under `~/tcp-shared/datasets/` (or `--out`): README.md and PROMPT.md (how to use it, the one-line prompt "Use the attached TCP Facts. Answer: ..."), HOW-TO.pdf, facts.md (every fact on one line, grouped by topic, small enough for one chat), topics/<tag>.md, facts.jsonl (the export with its metadata), services.md (what the latest service description lists), prices/<region>.csv (the latest snapshot of each region), MANIFEST.json (counts, dates, sha256). Every text file opens with the rules for the assistant and the best-before date (30 days). The facts come from `awb kb export` (live, contract, docs; name checked); a test tenant alias is rewritten to "a TCP test tenant", proven on a planted alias first, and a folder with one left is removed again |
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
| `awb report --period --from 2026-04-01 --to 2026-09-30` | the period report: every project with activity in the period in three blocks (active customer work, finished customer work, technical questions), a summary above and the block rules at the end; codes only, `--format html` for a page |
| `awb report --period --from 2026-04-01 --to 2026-09-30 --reveal --out FILE` | the same with the customer names put back: owner only, your own terminal, never inside an assistant session; FILE (mode 600) outside every git working copy, the shared side, the knowledge base and the projects, or no `--out` for the terminal |
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
