# Architect Workbench

**A harness for cloud architecture work on T Cloud Public (TCP).** The Workbench sits around an AI assistant (Claude
Code). The model reasons and writes. The Workbench decides what the model may use, checks what it hands out and
keeps the record: checked facts in place of the model's memory, live prices, exact arithmetic, real test tenants and
a review before any text leaves.

You work in it as with a colleague in a terminal. You ask in plain words. The session looks the facts up, asks the
price API, builds on a test tenant when the work needs it and writes down what it found with a source for each
statement. A customer or partner appears only as a code (`CUST-Q7M4`), a piece of work as a project code
(`tcp-q7m4`).

Most of it is not tied to one cloud. The facts, prices and API details are for TCP.

## Two kinds of work

Every piece of work starts with `awb spawn` and gets its own folder with its goal, its rules, its checkpoints and
its git history.

| kind | for | reaches |
|---|---|---|
| query | a question: can TCP do this, what does it cost, how does a service behave | the knowledge base, the documentation, the service description, live prices, `awb calc` |
| project | a proof of concept, a lab, a migration plan | everything a query reaches, plus the test tenants |

A query that turns out to need a live test becomes a project with `awb projects kind tcp-xxxx project`. A query
never touches a tenant: the key service refuses its calls.

## Live tenants through the key service

A project builds and checks on real TCP test tenants. The session never holds a key. The key service runs on
the owner's side. It keeps the keys, logins and passwords of every test tenant in memory and signs each call a
session sends. The read key allows reads only. A write needs the lab key and the code of an active project.
A write to the identity service is always refused. Every answer comes back with every key and secret taken out.

Every resource a project creates carries the project code as a tag and a row in the project's `RESOURCES.md`. A
daily snapshot tells what ran on which tenant on which day. `awb close` refuses while something is still running.
What a session learned live on the platform goes into the knowledge base with the grade `live`.

## The exchange: files in and out

Files come in through two inboxes in object storage (OBS). You drop a file there and tell the session in your own
words which one you mean ("the pdf", "the newest", a part of its name). The session takes it with `awb inbox take`.

| inbox | for | what the session gets |
|---|---|---|
| the lab inbox | material without customer content: vendor images, test data, public documents | the file itself, after the name check |
| the owner inbox | anything that may hold customer material | only the sanitised copies of the owner's intake, never the original |

Results go back with `awb xchg put` into the project's own folder, after the name check and, for a customer
project, the send gate. A mail tells you about each file.

## The board and the console

Every project keeps its status in the first two lines of `STATE.md` (`Status:` and `Next:`). A session cannot end
a reply while its commits are newer than that status. `awb board show` turns the status of every active project into
one page for management: what each project waits on, its open items, its deliverables by review state and its live
resources, with codes only. `awb board write` keeps it as Markdown and as HTML that prints on A4.

The console is the web side of the same work, behind a sign-in. It lists the projects with their status, files and
deliverables, shows the board and what runs on the test tenants, creates a new query or project and imports chosen
files from the buckets into a project. Each project has a chat that reads its sanitised inputs. The Ask page answers
a general question in plain words only from the knowledge base, the live price API and the tenant snapshots.

## Deploy

A change goes live with one command after its commit:

```bash
awb deploy --dry-run    # the plan, nothing runs
sudo awb deploy
```

The deploy installs the commit as a release of its own and restarts only what the change touched. The name check
and the key service move to the new code without a lock, so no session stops and no key is loaded again. Changed
rules reach every project and every running session. The run ends with the status of each service.
`awb deploy status` shows what runs where, `sudo awb deploy --rollback` goes back to the release before.

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
regions) carry an expiry date and are checked again.

**Only what is offered.** A service counts as offered only when the latest service description lists it. The docs,
an API that answers or a price record do not make it offered. `awb service check` tells, both chats carry the list
and the review refuses a deliverable that names a service which is not offered.

**Whole-solution prices.** Anyone can look up one price in a calculator. The Workbench prices the whole solution:
every position fetched live from the public price API and a price sheet recomputed to the cent before it leaves.
Every total, sum and saving is computed by `awb calc` with exact decimals. The model never adds numbers in its head.
The review accepts a computed number only together with its recorded calculation.

**Migrations in phases.** `awb migrate` takes an Azure inventory (a sanitised copy of the customer's spreadsheet) to
a reviewed plan: discover, map, estimate, plan, review. Every machine gets the nearest TCP flavor that is not smaller
on vCPU and memory, from the live price API, with a reason for every row that needs a decision. Each phase is done
by a worker with only the rights it needs and passes a gate before the next one starts.

**Reviewed before it leaves.** `awb review` turns a deliverable into a list of claims, each with its evidence. For an
important text a review board reads it as an architect of the source platform, an architect of TCP and the partner
who has to sell it, plus a check for invented facts and a misread question. The answer is rewritten from the
question, never patched. A send gate lets only the exact reviewed version leave.

**Texts in the architect's own voice.** `awb write check` measures a text against the architect's style and a
drafting skill writes mails, offers and PoC documents in that voice, with a check that the voice pass changed no
fact.

**Few questions.** A session asks at most one question per reply, at its end, with the default it takes. Every other
open decision goes into the project's `OPEN.md` with its default. `awb report --by questions` counts the replies that
ended with a question.

## Install

You need a Linux host (tested on Ubuntu 24.04 with Python 3.12), `git`, `gpg`, the poppler tools (`pdftotext`,
`pdfinfo`, `pdfimages`, `pdfdetach`) and Claude Code with your own subscription login. The `awb` command itself
needs no model and no API key. Runtime code is the Python standard library plus the poppler tools.

```bash
git clone https://github.com/shimoza/tcp-awb.git ~/tcp-awb
cd ~/tcp-awb
python3 -m venv .venv
.venv/bin/pip install -e . pytest python-docx openpyxl reportlab
.venv/bin/pytest
.venv/bin/awb gate --selftest
```

The tests use invented names only (`tests/fixtures.py`) and a throw-away Workbench each. They never touch your own
folders.

### As a plugin for Claude Code

The skills, the workers and the checkpoints also come as a plugin, with this repository as its marketplace. Install
`awb` first (above), then:

```bash
claude plugin marketplace add shimoza/tcp-awb
claude plugin install awb@tcp-awb
```

| part | what it does |
|---|---|
| `/awb:tcp-facts` | answers about TCP services, limits, flavors, regions and APIs from checked facts |
| `/awb:tcp-price` | live prices and price sheets checked to the cent |
| `/awb:review` | the review of a deliverable before it leaves |
| `/awb:test-tenant` | the test tenants: what runs, API calls through the key service, tags for new resources |
| `/awb:exchange` | files from the inboxes into a project and results back to the owner |
| `/awb:azure-to-tcp` | Azure machines to TCP in phases, from the inventory to a reviewed plan |
| `/awb:drafting` | texts in the architect's own voice (the style is the author's: replace it with yours) |
| workers | `migration-worker-files` (files only) and `migration-worker-shell` (files and `awb` commands), one phase each |
| hooks | five checkpoints: every prompt, before and after a write, the start and the end of a session |

On a sealed host (below) the seal installs the hooks for the work user already. Do not install the plugin there as
well: every hook would run twice.

## The main commands

The full list, with every option and who runs what, is in `COMMANDS.md`.

| task | command | what you get |
|---|---|---|
| start work | `awb spawn query --goal "..."` or `awb spawn project --goal "..."` | a folder with its scope, rules, hooks and git. A query works with the knowledge base, the docs and live prices; a project also reaches the test tenants. `--customer CUST-XXXX` on either |
| switch | `awb projects kind tcp-xxxx project` | a query that needs a live test becomes a project (or back) |
| see the projects | `awb projects list` | every project with its goal and the session that holds it |
| finish a project | `awb close tcp-xxxx`, then `awb projects delete tcp-xxxx` | close waits for open items and live resources; delete removes the folder and keeps the code |
| take a file | `awb inbox take the pdf` | the file you described, from either inbox, into the project |
| look up a fact | `awb kb find "ecs flavor eu-nl"`, `awb kb show KB-XXXX` | ranked facts with grade, source and check date |
| offered or not | `awb service check dms rds` | the section and revision of the latest service description |
| a price | `awb price find ecs --grep s3.large` | the live records with every term |
| check a price sheet | `awb price check sheet.csv` | every price fetched again, every total recomputed |
| compute | `awb calc "730 * 0.0418 * 3" --places 2` | the exact result, recorded as `K-N` for the review |
| the test tenants | `awb cloud tenants`, `awb tenant now test-1` | the tenants by alias and what runs there |
| call the TCP API | `awb cloud call GET vpc "/v1/{project_id}/vpcs" --tenant test-1` | the answer, signed by the key service; a write needs `--role lab` and runs for the project of the folder |
| a migration | `awb migrate init --from azure`, then `awb migrate next` | the next phase, its worker and its files |
| review a text | `awb review init`, `claims`, `l0`, `pass` | the claim list with evidence and a review record |
| the week | `awb ledger add ...`, `awb report --by customer` | one line per piece of work, reports by customer, technology or project |
| the status | `awb board show`, `awb board write` | the status of every active project on one page |
| bring a commit live | `sudo awb deploy` | the new release, only the changed services restarted, the status at the end |
| publish the console page | `sudo awb web publish --from-queue ID` | the checked page of a UI run live, the old one kept as a backup |
| hand a UI task to Codex | `awb ui submit presentations/ui-tasks/ID.txt`, `awb ui status` | the task checked and queued; a result note in `presentations/ui-tasks/` when the run ends |

## A typical day

1. `awb spawn project --goal "Test a virtual firewall appliance on TCP"` and open a Claude Code session in the new
   folder.
2. Drop the vendor image or the customer's spreadsheet into an inbox and tell the session which file you mean. It
   takes the file with `awb inbox take`.
3. Ask in plain words. The session reads the project's SCOPE, STATE and OPEN files, looks facts up with `awb kb`,
   prices with `awb price` and `awb calc` and writes what it found to `evidence/`, with a source for each statement.
4. When it builds on a tenant, it calls through the key service with the project code. Every resource goes into
   `RESOURCES.md`.
5. A text for a customer or a partner goes through the drafting skill and `awb review`. Only the reviewed version
   leaves.
6. The session writes its ledger entry, commits and harvests by itself: what it learned live on the platform goes
   into the knowledge base with the grade `live`. When it called the TCP API and added nothing, the stop hook sends
   it back once to do so. It keeps the two status lines of `STATE.md` current for the board. At the end,
   `awb close` checks that nothing is left running.

## Test tenants and keys

A working session never sees a key. The key service (`awb keys serve`, a systemd service of the owner) holds the
keys and secrets of every test tenant in memory and signs the calls the sessions send it. The owner keeps them in
the password store (`pass`), one folder per tenant alias:

| entry | what it is | what it allows |
|---|---|---|
| `awb/tenant/<alias>/ak`, `sk` | the read key, best an IAM user with a read-only role | GET and HEAD |
| `awb/tenant/<alias>/lab/ak`, `lab/sk` | the lab key | every method; a write needs the code of an active project |
| `awb/tenant/<alias>/secret/<name>` | a login or a password | fills a password field of a request, never shown |

```bash
pass insert awb/tenant/test-1/ak
pass insert awb/tenant/test-1/sk
awb tenant add test-1 --keys pass:awb/tenant/test-1 --region eu-de
awb keys unlock                  # after every start of the service
```

A request body may name a secret only as the whole value of a password field, `{"admin_pass": "{{secret:NAME}}"}`.
The answer comes back with every key and secret of the tenant taken out, also in base64 and JSON form. Every call is
logged on the owner's side without its path or body.

`awb tenant snapshot` records what runs on each tenant every day, so that `awb tenant at test-1 2026-09-30` tells
what ran on a given day and `awb tenant project tcp-xxxx` what a project created. Calls keep a pause between them and
wait when the API gateway says so (HTTP 429).

## Files in and out

You drop a file into `inbox/` of the lab bucket or of the owner bucket in the OBS console, with no project code
and no command. Then you describe it to the session in your own words. `awb inbox take` matches your words against
the file names inside the key service: the same letters, a kind of file ("the pdf", "the spreadsheet"), "the
newest" or a part of the name. One match is taken. None or several list both inboxes, without any name of the owner
inbox. The session takes the one you mean with `--id`. `--all` takes every file of both inboxes. A file of the
owner inbox goes through your intake and the session gets only the sanitised copies; the original goes to the vault.

A file with a name hit or unknown name candidates is held. You get a mail about it. Results come back with
`awb xchg put` into `<project>/from-session/<date>/` of the lab bucket, after the name check and, for a customer project, the
send gate. You fetch them in the OBS console. A mail tells you about each one. The key service signs every object call
and refuses everything outside the inbox and the project's own folders.

## The knowledge as a dataset

The knowledge base of TCP facts is not part of this repository. A team that builds its own tools can take it as a
dataset:

```bash
awb kb export --out tcp-facts.jsonl
```

One JSON object per line:

```json
{"id": "KB-XXXX", "scope": "tcp", "statement": "...", "grade": "live", "checked": "2026-09-25",
 "expires": "2027-03-24", "expired": false, "class": "api", "tags": ["ecs"], "source": "...",
 "negative": false, "tried": []}
```

By default the export carries `live`, `contract` and `docs` facts only. Statements of the product team (`said`) may
be confidential and need a clearance first (`--grade said`). Assumptions stay out. Retired facts never leave and
expired ones only on request (`--include-expired`, marked `"expired": true`). Every entry passes the name check once
more before it is written.

A dataset ages. Facts about availability expire after 30 days, API facts after 180. Use the `checked` and `expires`
fields. Take a fresh export rather than an old one.

## Customer data

The Workbench runs on whatever model the architect's assistant uses. That model may be public. With a key to a
private model, customer data may go in and nothing needs to be hidden. On a public model the harness keeps live
customer data out of the model as far as it can:

- `awb intake` turns documents into working copies with a code (`CUST-XXXX`) in place of every registered name. It
  also takes out bank data, tax ids, phone numbers, addresses, secrets and hidden text. It is tested with 1,188
  invented cases (`calibration/redteam/`).
- The register of names lives in a vault on the owner's side. The sealed setup runs every session as a separate
  system user that cannot read it.
- Five hooks check every prompt, every written file and the start and end of every session, so that a registered
  name typed by mistake is stopped before the model sees it. While the name check cannot run (a locked vault),
  every prompt is refused with the time of the lock.
- A gate checks every commit for names, secrets, home paths and the owner's blocklist. A send gate lets only the
  exact reviewed version of a deliverable leave.

These checks are a safeguard, not a guarantee. The architect stays responsible for what goes into a session.

## The sealed setup and its operation

The seal uses two users on one host. You (the owner, with `sudo`) keep the register, the keys and the intake. The
work user `awb` (no `sudo`) runs every Claude Code session and owns the projects. `seal/README.md` explains the
design and its limits.

```bash
sudo seal/setup.sh --dry-run     # prints every step, changes nothing
sudo seal/setup.sh               # add --mirrors DIR... for read-only doc mirrors
sudo seal/verify.sh              # every line must say PASS
awb vault encrypt                # the register encrypted at rest
ssh awb@<host>                   # the work user takes your ssh keys
awb seal check                   # as awb: what the work user can and cannot reach
```

The setup installs the vault daemon, the key service and the web services from the unit templates of `seal/`.
Things you want no commit to carry (names of other workspaces, a naming scheme) go into a local blocklist, one regular expression per
line: `/etc/awb/blocklist.txt` or `~/.config/awb/blocklist.txt`. It never enters the repository.

| when | what to run |
|---|---|
| after a reboot | `awb vault unlock`, then `awb keys unlock` (as the owner) |
| after a commit | `sudo awb deploy`: the vault and the keys stay unlocked |
| after a library upgrade | `sudo awb deploy --only awb-vaultd.service awb-keyd.service` |
| a new project, close or delete | as `awb`: projects belong to the work user |
| a customer's material | as the owner: `awb register` the names, `awb intake --customer CUST-XXXX`, then `awb spawn ... --from-outbox` as `awb` |

## Layout

| folder | what |
|---|---|
| `awb/` | the package and the `awb` command; `awb/tcp/` holds what belongs to TCP |
| `rules/` | one rule per file with its reason and what enforces it, plus the data files of the checks |
| `hooks/` | git hooks: `pre-commit`, `commit-msg` and `pre-push` run the gate |
| `seal/` | setup and verification of the two-user seal, the service units, the work user's client settings |
| `plugin/` | the Claude Code plugin; `.claude-plugin/marketplace.json` makes this repository its marketplace |
| `workflows/` | the review and refresh workflows for Claude Code |
| `calibration/` | the review calibration set, the red-team pack and the records of the red team and the security review |
| `tests/` | every test, invented names only |
| `docs/api/` | the contract of the web API (`awb api check`) |
| `COMMANDS.md` | every `awb` command by who runs it |
| `INTERFACES.md` | the interface of every module |
| `CHANGELOG.md` | what changed, one entry per week |

## Personal parts

- `rules/voice.txt`, the drafting skill and `tests/test_his_voice.py` carry the author's own writing style, measured
  from his own typed English. Replace them with yours.
- The knowledge base of TCP facts is not part of this repository. `awb kb` works on an empty one.

## License

The Workbench is licensed under the Apache License 2.0. The text is in `LICENSE`, the copyright in `NOTICE`.
