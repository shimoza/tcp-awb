# The fact-check service: design of 2026-10-14

Design only. Built on the pilot of today (`FACT-CHECK-PILOT-2026-10-14.md`, project tcp-n7e6 on the lab test tenant), the
recheck of today (`KB-recheck-2026-10-14.md`, 49 entries left expired), `awb/kb.py`, `awb/tcp/lab.py`,
`awb/tcp/sweep.py`, `awb/tcp/refresh.py`, `awb/grants.py`, `seal/egress/`. Where a choice was open the smallest
sound option is taken and named. Nothing here reads a name; every text carries codes.

## What the service is

tcp-n7e6 stays the standing service project (kind project, no customer, granted on the lab test tenant). A command,
`awb kb recheck-run`, takes the expired entries whose check block it can run, runs them in that project, proves
the tenant as it was and writes the result back into the knowledge base. What it cannot run it lists for the owner
with the reason. There is no money cap. The guards are the lifetime, the flavor deny list, one recipe at a time
and the inventory before and after.

## 1. The fact format: a check block

An entry file keeps its header and its statement unchanged. A second section follows the statement after a line
`--- check`, `key: value` lines like the header. `parse` ends the statement at that line; an entry without the
section parses as today, so nothing of the 400 entries changes until a block is written. `render` writes the block
back in a fixed order; `verify` checks it like the header (fields known, tier known, a write tier names a recipe
that exists, a decision tier names what it needs). `awb kb how ID -` writes or replaces the block from stdin
through the text checks of `add` (English, no name, no price, no secret); `awb kb show` prints it.

| key | values | what it says |
|---|---|---|
| `tier` | `read`, `write`, `decision`, `none` | who may run it (section 2) |
| `event` | an ISO date, optional | the statement records an event of that day; a run adds the state of the new day and never says the event is false (section 4) |
| `calls` | read tier: one or more `GET service path`, `;`-separated; write tier: `recipe NAME k=v ...` | what is sent, through the key service only |
| `confirms` | one line | the answer that proves the statement |
| `refutes` | one line | the answer that proves it wrong |
| `control` | one line | the call or the field that tells a confirm from a refute, so an answer that is the same for both (the 500 of KB-TAXN, the WAF.1001 of KB-BS7A) counts as undecided and never as confirmed |
| `needs` | one line, `none` or what the host cannot give | a person at a console, a second domain, a stock change over time, a resource with a floor on its life, a POST the read key does not allow |

Without `control` a block is refused: the pilot showed that a check without one proves nothing. An entry whose
`needs` is not `none` gets the tier `decision` or `none`, never `read` or `write`.

```
id: KB-P2E2
scope: tcp
tags: smn
grade: live
checked: 2026-10-14
class: api
expires: 2027-04-12
source: evidence/recheck-2026-10-14/KB-P2E2.md of tcp-n7e6
---
An HTTP subscription of an SMN topic receives a SubscriptionConfirmation message whose subscribe_url points at
the web console; a plain GET of that URL without a login confirms the subscription.
--- check
tier: write
calls: recipe smn-http-confirm flavor=s3.medium.1
confirms: the subscription reads status 1 after the endpoint's GET of subscribe_url
refutes: the status stays 0 after the GET or the GET answers a redirect to a login page
control: the status is read before the GET and must be 0 then; a 1 before the GET means another actor confirmed it and the run is undecided
needs: none
```

A read-tier block is the same with `calls: GET ecs /v1/{project_id}/cloudservers/flavors` and a `confirms` that
names the field and the value (`cond:operation:az of s9.medium.1 lists eu-de-01 as normal`), a `refutes` with the
other value (`sellout` or the size missing) and a `control` that must differ (`a size that is sellout in the same
answer, so a list that says normal for every size is undecided`).

## 2. The tiers and who triggers each

| tier | what it may do | key | who triggers | result written by |
|---|---|---|---|---|
| `read` | GET and HEAD through the key service, no resource | read key of the tenant | a timer as the work user, weekly | the run: `kb recheck` on confirm |
| `write` | a recipe of `recipes/` in tcp-n7e6: create, check, delete, prove gone | lab key, project tcp-n7e6 | the steering session, monthly, one command, run in the background | the run: `kb recheck` on confirm, the rest to the result file |
| `decision` | a write tier whose `needs` the owner has to give: a heavy resource, a second tenant, a long life, a per-service secret | lab key, after his word per entry | the owner, `awb kb recheck-run --id KB-XXXX --go` in his terminal | as write |
| `none` | nothing on the platform; a docs or contract read or a thing no run can show | none | `awb refresh` lists them as before | the steering session by hand |

A dated event (`event:` set) runs in its tier like any other block; only the write-back differs (section 4).

## 3. The run flow of `awb kb recheck-run`

`awb kb recheck-run [--tier read|write] [--id KB-XXXX]... [--project tcp-n7e6] [--tenant ALIAS] [--dry-run]
[--go]`. Default tier `read`; `write` needs the project of the kind project and its grant, as `awb lab run` checks
them today; `decision` runs only with `--id` and `--go`. The steps, each a line in the run's RESULT.md:

1. Pick: the expired entries (`kb.expired`) with a block of the tier, oldest expiry first. Entries without a
   block, with another tier or with `needs` not `none` go to the lists "no block", "other tier" and "for the
   owner" of the result and are not touched. `--dry-run` stops here and prints the recipes and their parameters.
2. Inventory before: `sweep.sweep` over SOURCES, NETWORK_SOURCES and the new NAMED_SOURCES (section 5, F3) with
   the read key: per type the count and the digest of the sorted id set, to `inventory-before.txt`. A listing
   that cannot be read stops a write run before any create (an unknown listing cannot prove equality).
3. Check, one entry at a time, never two recipes at once: a read tier sends its calls through the key service;
   a write tier runs the recipe as a lab run (the class Run of `awb/tcp/lab.py`) with the parameters of the block in tcp-n7e6, with the rows in
   RESOURCES.md, the tags and the teardown the lab already does. The verdict comes from `confirms`, `refutes`
   and `control`: confirmed, refuted or undecided (the control did not tell them apart, a 5xx, a timeout, a
   refusal before the first call).
4. Delete: the recipe's own teardown, proven gone with a read, as today. A row that stays live sets the run to
   exit 4 and skips every further entry; the watchdog (section 5) takes the leftover.
5. Inventory after: the same listing. Equal digests per type or the run ends with "inventory differs: TYPE
   +n/-n" (counts, never ids), exit 4 and step 6 is skipped for every write-tier entry of the run.
6. Write back, through the checks of the knowledge base: confirmed and no event: `kb.recheck` (grade live, source
   the evidence path, the tried texts of the new check for a negative); confirmed or refuted with an event: an
   amend that keeps the statement and adds one sentence "On DATE the state is ..." with `--checked DATE`;
   refuted or undecided without an event: nothing changes, the entry stays expired and the result holds the
   answers (status and error code, the first 200 characters of the message after the name check). An amend of a
   refuted fact needs a reader; the smallest option is the steering session with the second check of
   `workflows/refresh-verify.md`, never the run.

The evidence goes to `evidence/recheck-<date>-<n>/` of tcp-n7e6: RESULT.md, the two inventories, calls.log in the
form of the pilot (answers cut at 1500 characters, tokens and URLs with a token reduced to host and path), one
file per entry. The run writes one line to `<shared>/recheck/state.tsv` (date, tier, counts, exit, inventory
equal or not) for the board and the mail.

## 4. Dated events

An entry whose block carries `event: DATE` records what happened that day (KB-T6NU: the s9 create of 2026-07-20).
A run never marks it confirmed or refuted as a fact. It runs the block and amends: the statement keeps its date
and its event and gets the sentence with the state of the new day; `checked` moves to the run's date, so the
entry expires again after its class. A statement that already carries three such sentences gets the oldest state
sentence dropped, so an event entry does not grow without end.

## 5. The guards

- Flavor deny list, in `lab.validate` and at parameter time, so no recipe may carry one even as a default:
  any flavor family that starts with `p`, `pi`, `g`, `ai` or `kp`, any size token above `2xlarge`, any
  `physical` or bare-metal flavor, any RDS, DDS, DCS, CSS or MRS size above `2xlarge` or named `large.8` and
  above. The list is `recipes/DENY.txt`, one pattern per line, read by the validator; a recipe or a parameter
  that matches is refused with the pattern's line number, never the value. The pilot's s9 gap is the other
  direction: the `ecs-boot-ssh` pattern allows s2, s3, c4, m3 and t2 only, so the recipe of F7 allows every
  family that is not denied, up to `2xlarge`.
- Lifetime: a recheck run caps every recipe at 30 minutes (`guard.lifetime_minutes` of the recipe or 30,
  whichever is less), under the lab's 120. The `awb-expiry` tag and the name of an untagged resource carry
  the end of that lifetime, as today. One recipe at a time, so the most that lives at once is one recipe's
  guard (at most 12 resources).
- Watchdog and cleanup on a crash: `awb kb recheck-watch`, a root-owned timer every 15 minutes as the owner
  (like `awb-build-nudge.timer`). It sweeps the tenant with the read key and every resource tagged or named with
  the code tcp-n7e6 whose expiry has passed is deleted with the lab key (the teardown calls of the recipe that
  made it, by its handle in RESOURCES.md; a resource no row names is deleted by the type's delete call of the
  sweep's SOURCES) and proven gone. A leftover it cannot delete goes into one mail with the handle and the type.
  A run killed hard (the pilot's 30-minute rule) is therefore cleaned within 15 minutes of its expiry.
- Inventory: section 3, steps 2 and 5. The named sources (F3) cover what the sweep does not read today: WAF
  policies, SMN topics and subscriptions, RDS instances, found by the project code in their name.
- Egress: nothing changes. The run opens no connection of its own; every call goes over the key service's
  socket, so the work user's chain never sees it (the pilot's 87 calls counted nothing). The SMN check reaches
  its endpoint from the ECS inside the cloud, never from the host. No row is added to `ALLOW.md`.
- Secrets: a recipe that needs a password the key service does not hold (SQL Server refused ecs-admin,
  DBS.280246) fills `{{random:password}}`: the key service draws a throw-away value that meets the field's rule
  at call time and never returns it; the session never sees it. A per-service secret (`rds-admin`) stays an
  option the owner adds with `awb tenant` when a check has to log in later.

## 6. The schedule

The read tier runs weekly, Sunday 02:30 UTC, as the work user (`seal/work-jobs/awb-kb-recheck`, beside the
nightly kb commit; at night because of the name check budget). The write tier runs monthly, on the first Monday,
started by the steering session (`sudo -n -u awb -H awb kb recheck-run --tier write`, in the background), and
again on request after a recipe or a block was added. The decision tier runs when he says, per entry. The expiry
classes stay what they are; nothing is checked before it expires. Monthly is chosen over "on expiry" because a
recipe run costs an inventory and a teardown each time; a batch does both once.

## 7. How results reach the owner

- The board carries one line from `state.tsv`: `recheck: read 2026-10-18 12 confirmed, 1 refuted, 3 undecided;
  write 2026-11-02 4 confirmed, inventory equal; 9 for the owner` (fixed vocabulary, counts only, like the build
  line).
- A mail, sent by the watchdog as the owner (the work user has no mail path): once when a write run ends or
  stops, with the counts, the evidence path and the next command when something waits on him (`awb kb
  recheck-run --id KB-XXXX --go`); at once when a leftover resource could not be deleted.
- The prompt hook shows the board line at his next message, as it shows the build line.

## 8. What stays his

- The grant of tcp-n7e6 (`awb projects kind tcp-n7e6 project`, done for the pilot) and a second tenant's grant
  when a block needs two domains.
- The decision tier: each `--go`, after reading the entry's `needs` line and the recipe's cost guard. The 24
  heavy entries (GPU, MRS, DLI, CCE, DRS, HA RDS) and the 11 decision entries of the pilot stay his.
- A per-service secret in the key service and a second domain's key (`awb tenant`).
- The deploy of each task and `awb projects sync` after the rules change.

## 9. Migration of the expired entries

Today 49 entries are left expired after the recheck and 2 were amended by the pilot; with KB-BJMP (withheld) about
48 need a block. The steering session writes them with `awb kb how ID -` from the pilot's lists, at night, in
batches of ten:

| group | entries | block |
|---|---|---|
| heavy resource | 24 (KB-ARK3, KB-EDXJ, the 9 MRS, KB-NSFM, KB-HYY7, KB-NFFJ, KB-GZHR, the 8 DRS, KB-H7UG) | `tier: decision`, `needs:` the resource and its least size, no recipe yet |
| decision, not heavy | 11 (KB-N72X, KB-LJFS, KB-YPPS, KB-DOPL, KB-RY24, KB-PAUD, KB-XXAD, KB-I4TR, KB-TAXN, KB-6FEI, KB-MQ2H, KB-BUI2) | `tier: decision`, `needs:` the floor, the second tenant, the person or the stock change |
| recipe exists after F7 | KB-P2E2, KB-T6NU (event), KB-37RS (API part only, the appliance part in `needs`) | `tier: write`, `calls: recipe ...` |
| not checkable from this host | 9 (KB-HT72, KB-VUL6, KB-6XCS, KB-EFI6, KB-DIDW, KB-IP74, KB-46JJ, KB-RGV4, KB-BS7A) | `tier: none`, `needs:` the POST, the region, the signature, the console form, the SDK, the WAF rule body |
| rest, a GET shows it | the entries the recheck of today rechecked live go next time through `tier: read` | written when they expire again, 30 or 180 days on |

The two groups "decision" cover 12 ids because KB-TAXN sits in both lists of the pilot; it is one block.

## 10. The build tasks, ranked

One build-queue task each, paths chosen not to meet. Effort S under a day, M a few days. Every task adds its
CHANGELOG line, its README or COMMANDS.md lines and its rows in `build/tests.json`.

| label | change | files | effort | tests |
|---|---|---|---|---|
| F1 | the check block: parse, render, verify, `awb kb how ID -`, `show` prints it, `tier`, `event`, `needs`, a block without `control` refused | `awb/kb.py`, `tests/test_kb.py` | M | an entry without a block parses as before; a block round-trips; `how` refuses a block without control, with a name, with a price; `verify` refuses a write tier naming no recipe |
| F2 | the recipe guards and format: `recipes/DENY.txt` in `validate` and at parameter time; the 30-minute cap when a run says so; `{{random:password}}` in a body | `awb/tcp/lab.py`, `awb/tcp/keys.py`, `recipes/DENY.txt`, `recipes/README.md`, `tests/test_lab.py`, `tests/test_keys.py` | M | a GPU default is refused; `p` family and `4xlarge` as parameters are refused by line number; a random password never appears in the answer or the evidence |
| F3 | named sources of the sweep: WAF policies, SMN topics and subscriptions, RDS instances, found by the project code in the name; the id-set digest per type | `awb/tcp/sweep.py`, `tests/test_sweep.py` | S | a fake listing with a policy of the code is found; an unreadable listing reports unknown; equal sets give equal digests, one id more does not |
| F4 | `awb kb recheck-run`: pick, inventory, check, delete, inventory, write back, RESULT.md, `state.tsv`; `--dry-run`, `--id`, `--go` | `awb/tcp/recheck.py` (new), `awb/cli.py`, `tests/test_recheck.py` | M | with `lab_fake` and `tcp_fake`: a confirm rechecks, a refute leaves the entry expired, an undecided control writes nothing, an unequal inventory skips the write-back and exits 4, an event is amended with the date |
| F5 | `awb kb recheck-watch`: the reaper of expired tcp-n7e6 resources and the mail, a root-owned timer as the owner | `awb/tcp/recheckwatch.py` (new), `seal/setup.sh`, `seal/verify.sh`, `tests/test_recheckwatch.py` | M | an expired tagged resource is deleted and proven gone; one that cannot be deleted mails once; a run end mails once per `state.tsv` line |
| F6 | the board line from `state.tsv`, fixed vocabulary | `awb/tcp/board.py`, `tests/test_board.py` | S | a line with the counts; a cell outside the vocabulary is dropped |
| F7 | the recipes: `ecs-flavor-boot` (every family not denied, console read, the s9 case), `smn-http-confirm` (the pilot's check B as a recipe), `rds-create-refusal` (the collation probes with the invented control, no instance) | `recipes/ecs-flavor-boot.json`, `recipes/smn-http-confirm.json`, `recipes/rds-create-refusal.json` | M | `awb lab dry-run` of each; the validator passes them; the deny list refuses a GPU parameter on the first |
| F8 | the refresh worklist carries the tier and the block; entries without a block count as overdue in the knowledge part | `awb/tcp/refresh.py`, `tests/test_refresh.py` | S | the worklist line of an entry with a block names its tier; an expired entry without a block is counted |
| F9 | the weekly read-tier job of the work user | `seal/work-jobs/awb-kb-recheck.service`, `.timer`, `seal/setup.sh` (after F5 is merged) | S | `seal/verify.sh` proves the timer enabled |
| F10 | the migration of section 9: the blocks of the 48 entries, written at night by the steering session | the entries of the knowledge base, a list in `presentations/knowledge/RECHECK-BLOCKS-2026-10.md` | M | `awb kb verify` over every changed entry; `awb kb recheck-run --dry-run` lists them by tier |

Order: F1 and F2 first (nothing else parses a block or refuses a flavor without them), then F3 and F4 together,
F7 beside them (recipes only), then F5, F6, F8, F9 and last F10, which needs F1 live. F1, F2, F3, F4, F6 and F8
are queue tasks; F5 and F9 touch `seal/setup.sh` and go one after the other; F7 and F10 are steering-session work.

## Open decisions for the owner, with the default taken here

- D1 Schedule: read tier weekly on Sunday 02:30 UTC, write tier monthly on the first Monday. Taken unless he
  names other days.
- D2 A refuted fact is never retired or amended by the run; it stays expired with the answers in the result and
  the steering session writes the amend after the second check. Taken.
- D3 `{{random:password}}` in the key service in place of a per-service secret. Taken; a stored `rds-admin`
  stays open for a check that has to log in.
- D4 The flavor line: no family `p`, `pi`, `g`, `ai`, `kp`, no size above `2xlarge`, nothing physical. Taken; a
  decision-tier entry that needs more (the GPU driver facts) runs only with his `--go` and still never on a GPU
  flavor, so KB-ARK3 and KB-EDXJ stay expired until he lifts that line for one run.
