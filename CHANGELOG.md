# Changelog

What changed in the Architect Workbench, one entry per week (ISO week, Monday to Sunday), newest first. The form
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Customers and projects appear as codes only.

## Week 42, 2026-10-12 to 2026-10-18 (in progress)

### Added

- `INSTALL.md`, "Deploy your own Architect Workbench": the steps for an architect who runs the Workbench on his own Ubuntu 24.04 host (what to have, the order with the exact commands, what the setup creates, how to check, update, roll back and remove it, what is optional), linked from the top of `README.md`.
- A comment above `main` in `awb/build.py` names `awb/buildrun.py` as the runner of the queue (B-001).
- `awb pricebook build SPEC [--out FILE] [--lang en|de] [--against OLD]` builds the price workbook of an estimate from
  a specification file (line items with service, flavor, region, quantity, hours, pricing mode and an assumption).
  It refuses a service the service description does not offer, prices every line from the public price API with exact
  decimals and writes an .xlsx with the standard library only: the lines with their figures as formulas, the
  assumption of each line in its own column, a block of totals, a sheet Sources (snapshot time, region, service
  description revision) and, with `--against`, a sheet of changes. English or German. A Markdown summary for a mail
  (totals, the three biggest lines, assumptions) comes next to it and the totals are recorded with `awb calc`.
  `awb pricebook diff OLD NEW` lists what changed between two specifications or two workbooks, line by line and in
  the totals (work profile: prices were the top cost, one estimate was rebuilt 7 times).
- Lab recipes: a live check on a test tenant as one command (the work profile: 391 live checks in 45 of 86
  projects). `awb lab run RECIPE --project CODE --tenant ALIAS [--param K=V]...` checks the project's grant, tags
  and records every resource in RESOURCES.md as it is made, runs the checks and always tears down (also on a failed
  check, an error, the time budget and Ctrl-C or SIGTERM), proves each resource gone with a read and writes
  `evidence/lab-<recipe>-<date>-<n>.md` with a ready `awb kb add` line. A resource it cannot delete stays live with
  the line to delete it and the command exits 4. `awb lab list` and `awb lab dry-run RECIPE` (no call made). The
  format is in `recipes/README.md`; the first recipes are `ecs-boot-ssh`, `obs-policy-deny` and `elb-health`, their
  unproven API shapes marked "to confirm live".
- The dataset TCP Facts says what its facts cover: README.md, PROMPT.md and HOW-TO.pdf carry "What the facts cover",
  counted from the facts at each build (the orderable services with a plain fact per section of the service
  description, the requirement areas with their fact ids, an example of checking a requirement and citing the id,
  and the contract matters the dataset never holds).
- The release notes of a dataset release say what is new since the previous release: facts added, amended and
  retired against the `facts.jsonl` of the previous release tag, and the requirement areas and service sections the
  added facts touch.
- `awb dataset put --github-only` publishes to GitHub without the bucket and asks for no bucket key; `--github`
  still means bucket and GitHub.
- `awb import CODE --paste [--kind mail|transcript|table]`: the same one door for text he copies from a mail client, a
  meeting tool or a spreadsheet (work profile: 37 of 86 projects had customer data slip into a session, almost always
  as a pasted mail with a signature, a pasted transcript or an Excel extract). The text comes on standard input and is never
  echoed (a pipe from the clipboard or pasted into the terminal and ended with Ctrl-D with the echo off). It runs
  through the wipe-mode intake of the project's customer: the customer forms become its code and every other person,
  company, place, mail address and phone a token. A secret refuses the whole paste with its class and line number and
  writes nothing. A mail loses its signature block and its quoted chain headers as a whole (`[signature]`,
  `[mail header] from [person 2] to [person 1]`). A transcript keeps its turns as `[person 1]`, `[person 2]` by the
  first turn and its timestamps and drops the filler lines of the meeting tool. A table (tab-separated) keeps its
  columns as CSV and loses the columns of people and mail addresses as a whole. The copy lands in the outbox as
  `mail-<date>-<n>.md`, `transcript-<date>-<n>.md`, `table-<date>-<n>.csv` or `paste-<date>-<n>.md` (no kind), the sessions
  hear of it at their next prompt and the reports are written as for a file. The paste is not kept, so `--redo` does not
  take it again. Refused for the work user and inside an assistant session with `held: a pasted mail or transcript goes
  in through awb import tcp-xxxx --paste in your own terminal`.

### Changed

- The MCP server of the dataset (`tcp_facts_mcp.py`) follows the Model Context Protocol revision 2026-07-28:
  `server/discover`, the version and the capabilities in the `_meta` of each request (no handshake; an unknown version
  answers -32022 with the versions it speaks; the handshake revisions stay), `resultType`, `ttlMs` and `cacheScope`,
  server info in the `_meta` of each result. From 2025-11-25 a bad argument is a tool result with `isError` (an older
  session keeps the JSON-RPC error). Tools carry a title and read-only annotations. A null or boolean id and a message
  with neither method nor result are invalid requests, a lone surrogate no longer stops the server and a closed stdout
  ends it quietly. The "MCP clients" section of the dataset README says where Claude Desktop keeps its file, that it
  needs a full restart and how to write a Windows path. Checked on Python 3.10 to 3.13 and with the `mcp` Python SDK
  2.3.0 as client (tests MCP-14 to MCP-21). A request of the revision 2026-07-28 that asks for `ping` gets -32601, as
  that revision removed it (the handshake revisions keep it), and `server/discover` answers a full result with
  `resultType` and server info in every era (test MCP-22).
- `awb lab run` reads the EVS disk types (`GET evs /v2/{project_id}/types`, read key) before its first create when a
  create body names a disk type (`volumetype` or `volume_type`) and stops with one line, making nothing, when the type
  is not listed, not offered in the recipe's AZ (`RESKEY:availability_zones`) or sold out there
  (`os-vendor-extended:sold_out_availability_zones`). `awb lab dry-run` shows the check as a step. The recipes
  `ecs-boot-ssh` and `elb-health` use SAS for the system disk: SATA is sold out in eu-de-01 to eu-de-03 and run 3 of
  2026-10-12 made three resources before the server create answered Ecs.0044.
- The test suite runs faster with every test and every check kept. The stand-in servers of the tests shut down at
  once. The retry waits of the fetch code and the repeated self-test of the intake (for an untouched engine) are not
  waited out. The tests that must stay long carry the marker `long` with the reason.
- The prompt header of a build task tells the agent to run only the module tests that need no network, with the
  venv's interpreter and leaves the whole suite to the runner (B-001). The session's unit keeps no loopback: in the
  host's network namespace `127.0.0.0/8` reaches sshd and the web units; a namespace of its own would cut the
  model endpoint.
- The runner opens the `CHANGELOG.md` entry of a new ISO week on `main` of `main.git` before the first task of that
  week, as its own commit with your identity (the old week loses `(in progress)`), so check 8 no longer refuses the
  first task of a Monday. When the commit cannot be written the task is `held: changelog week` and starts again at
  a later poll.
- The work rules point a session at the paste door (`a pasted mail or transcript goes in through awb import CODE
  --paste in his own terminal`) and the prompt hook adds that line to the stop of a prompt with a pasted block. The
  notice of the sessions takes the names of pasted copies (`mail-2026-10-12-1.md`) beside file ids. The first line of
  `seal/work-claude/CLAUDE.md` is no longer followed by a blank line, so that the rules stay within their 60 lines.
- The help line of `awb build` describes the command as it is: the task queue (add, list, status, show, answer,
  retry, widen, stop) and its runner (serve, selftest, probe). It said that the queue comes later.
- `rules/tags.txt` has a tag for every service of the service description in `rules/services.tsv`: 33 new ones such
  as `functiongraph`, `taurusdb`, `geminidb`, `ddm`, `oracle-optimized`, `apig`, `ucs`, `ocr`, `anti-ddos`, `dbss`,
  `financial-dashboard`, `consulting`, `marketplace`, `alert-service`, `das` and `cloud-connect`. `awb kb add`,
  `awb ledger add` and `awb spawn` take them now. A test fails when a service of the list has no tag.

### Fixed

- A dry run of `seal/setup.sh` or `seal/verify.sh` under the tests no longer reads the host file of the host the
  suite runs on. `AWB_SETUP_CONF=FILE` and `AWB_VERIFY_CONF=FILE` (dry run only, setup.sh also with its test-only
  install root, never as root) name the host file the plan keeps its values from; a FILE that is not there stands
  for a fresh host, and the plan still writes `/etc/awb/paths.conf`. The test helpers set both: since the host's
  egress mode became enforce, the dry run of the count mode test kept `enforce` from `/etc/awb/paths.conf` and failed.
- The build user's guard lets the venv's interpreter pass for a Read and a run (B-001). The venv's `python` is a
  link to the base interpreter in `/usr/bin` and the guard refused it as outside the clone, so an agent in a task
  could run no test. Only the link of `python`, `python3` or `python3.N` in the venv's `bin` to the base interpreter
  its `pyvenv.cfg` names may leave the venv; a Write into the venv, any other link out of it and the rest of
  `/srv/awb-build` stay refused. `awb hook selftest` plants the link as the host has it.
- The work user's `awb lab run` and `awb projects check` no longer die with a traceback on the owner's vault
  (the first live lab run, 2026-10-12). The grants check looked for `grants.json` in a vault folder the work user
  may not open and got a refusal from the system instead of an answer; it now counts as not readable, so the lab
  run goes on with its preflight and the check prints its normal lines. The fallback of `awb spawn` without the name
  check looks at the register the same way.
- `awb lab run` no longer loses a resource whose create got no answer, a timeout or a 5xx (review of pull request 2).
  The row in RESOURCES.md goes in before the create call, in the state creating with the name the resource gets.
  When the answer holds no id, the find of the step looks the resource up by that name: found, the row gets its id
  and the teardown deletes it and proves it gone; a find that answers without it marks the row not created with
  that answer as proof (it does not hold `awb close`); a find that cannot tell leaves the row live for the owner.
  Every create of the three recipes has a find by name now. `awb cloud sweep` also lists VPCs, subnets, security
  groups, shared ELB load balancers, listeners and pools and NAT gateways, reads the tags of the lab name
  `<code>-lab-<YYYYMMDD>-<rand>` and names a resource with a project tag that no RESOURCES.md lists; dedicated ELB
  is not swept yet and the output says so.
- `awb import CODE --paste FILE` refuses the FILE with its own line on Python 3.12 too (the host's interpreter): the
  options and the files of `awb import` are parsed intermixed, so a file after an option is no longer an
  "unrecognized arguments" error of the parser.
- `awb lab run` stops with one line and exit 2 on any error of the host in its preflight (the first live run of
  2026-10-12 ended in a traceback and exit 1, which reads as a failed check). The owner's `awb cloud sweep` no
  longer ends in a traceback when the shared handle file is the work user's: his handles go to
  `~/.local/state/awb/handles.json` and one line says so; a handle file that cannot be written is one line and
  exit 2.
- The recipe `ecs-boot-ssh` after the first live run: the subnet carries the tenant's two DNS servers with DHCP on,
  so the probe's first-boot script does not wait for DNS; the two servers are created without adminPass and need
  no tenant secret (to confirm live: a create without adminPass and without a key pair); the console is read on
  the `/v2/` path proven live, without a length limit; the time budget is 35 minutes.
- `awb lab run` after the second live run of `ecs-boot-ssh` (2026-10-12), whose server create answered 400 with
  nothing more to read: a refused or failed call keeps its error code and the first 200 characters of its error
  message in the evidence (from `error_code`/`error_msg`, `code`/`message` or `error.code`/`error.message`), through
  the secret masking and the name check (withheld when the check finds a name or cannot run), and the stop line names
  the step and the error code. A recipe that fills a field with `{{secret:NAME}}` asks the key service for the names
  of the tenant's secrets after the grant read and stops before its first create with one line such as `the secret
  ecs-admin is missing for test-10491`; the dry run names the secrets it needs. Both servers of `ecs-boot-ssh` carry
  `adminPass` from the tenant secret again (parameter `admin_secret`, default `ecs-admin`), as `elb-health` does; both
  recipes note "to confirm live" whether user_data still runs with adminPass set.
- The plugin text follows the writing rules. The skills `azure-to-tcp`, `exchange`, `review`, `tcp-price` and
  `test-tenant` and the plugin README had a comma before "and" or "or". `tcp-price` pointed to an "earlier
  version" where it meant an older estimate. `awb write check --mode doc` now finds no blocking tell in any file of
  `plugin/`.
- `COMMANDS.md` and `README.md` match `awb --help` again. COMMANDS.md lists what it missed: the UI queue (`awb ui`),
  `awb web check`, `awb xchg clean`, `awb report --by questions`, `awb hook pre-tool` and `selftest`, the target `en`
  of `awb write translate` and options such as `--lab-user`, `--restart`, `--replace`, `--max-age`,
  `--region`, `--table`, `--hours` and `--scope`. The README layout table names `.claude-plugin/marketplace.json`
  where it is (the plugin folder never held it) and adds `hooks-build/`, `build/` and `CLAUDE.md`.
- `COMMANDS.md` names `awb dataset put --github-only` in the row of `awb dataset put`, as its help and the README do
  (follow-up of pull request 5).
- `INSTALL.md` matches the code of the host: the dry run of `setup.sh` without sudo sets `SUDO_USER`, the build side
  lines of `verify.sh` fail until it is set up, the Ask key belongs to `awb-ask`, `nft` is needed, the plugin needs
  `awb` on the `PATH`, `awb projects open` starts a session, the web rollback and the removal fit a new host and
  the mirrors, the web units and their users are named (follow-up of pull request 6).

### Security

- No test can reach the host's real vault any more (finding of 2026-10-12: three invented codes of
  `tests/fixtures.py` stood, inactive, in the real register). `tests/conftest.py` works out the real vault folder
  and the admin and check sockets once at the start of a run (from the root host file, the defaults and the
  environment the run started with, without opening anything there) and an audit hook refuses any open, listing,
  change, bind or connect that reaches them while a test runs; the test then fails even when the code under test
  caught the refusal. `tests/test_vault_guard.py` proves the guard can fail.
- A build task's unit no longer reaches any port of the host itself (finding of 2026-10-12). Its allow list named
  the resolver of `/etc/resolv.conf`, which on this host is systemd-resolved's stub `127.0.0.53`, so the session
  unit could connect to sshd on `127.0.0.53:22`. The session and probe units now allow the upstream nameservers of
  `/run/systemd/resolve/resolv.conf` (the same link rules, never a loopback or another address of the host) and
  the endpoints and see a `resolv.conf` of those servers alone, written into the run folder and bound over
  `/etc/resolv.conf`. Without an upstream nameserver the queue is held as `no resolver for the task unit`. `awb
  build probe` adds three checks: the unit cannot connect to `127.0.0.1:22`, `127.0.0.53:22` or the host's own
  address on port 22. The suite unit keeps its own network namespace with loopback alone.
- The short probe units of `seal/verify.sh` no longer allow the nameserver of `/etc/resolv.conf` (the stub
  `127.0.0.53`): they run fixed commands and resolve no name (follow-up of pull request 7).

## Week 41, 2026-10-05 to 2026-10-11

### Added

- The suite runs from any path, read-only, with a shared venv (self-build step SB0). `seal/setup.sh --suite-venv
  DIR` builds the venv of a build (the system packages and the test extras, not the package), and the whole suite
  passes from a read-only copy of the tree under `/var/tmp` with that interpreter by its absolute path, `-I -B`, the
  cache provider off and no bytecode written. `python-pptx` joins the test extras; the tests used it already.
- The build user of the self-build (step SB1). `seal/setup.sh` and every deploy set up `awb-build` (a system user in
  its own group alone, no password, no sudo), its folder `/srv/awb-build` on a 20 GB loop image (`main.git`, the
  clones, the runner's records, the suite's venv, an immutable home template with its own client settings), a
  pinned root owned copy of the client in `/opt/claude-build` and the runner's unit `awb-buildd.service`, installed
  and not enabled yet. `awb hook pre-tool` keeps the build user inside its own clone and fails closed; the build
  clones get commit hooks that run the installed gate. `/etc/awb/paths.conf` and the blocklist are now 640 root:awb.
  A deploy never starts the build runner and defers its restart while a build task runs. `sudo seal/verify.sh`
  proves it, as the build user and in a unit with the properties of a task.
- The build queue, its records and its commands (self-build step SB2). A build task is a block in
  `build/queue/QUEUE.md` (untracked) with a prompt file beside it; `awb build add FILE` checks the block's grammar and
  the gate's classes with the register and writes both atomically. `awb build list`, `status [--ended]`, `show ID`
  (your own terminal only), `answer ID TEXT`, `retry ID`, `widen ID PATH...` and `stop [ID]` read and write the
  numbered run records under `/srv/awb-build/runs/`. A refused block names the key, never the value. The runner
  that takes the tasks comes with SB3.
- The runner of the build queue (self-build step SB3). `awb build serve` takes the next task whose dependencies are
  done and whose paths are free, one at a time. It runs it as `awb-build` in units of one fixed `sudo -n
  systemd-run` form with a clock, a CPU share and no network but the model endpoint: a fresh home with the model
  key, a clone of `main` from a bundle, the headless session, the branch handed back as a bundle. It never runs git
  or code in the agent's clone. Eleven checks decide: the unit ended by itself, the client ended well, a `DONE:`
  line, one to five commits as you and nothing else, the gate with the register, the paths, the CHANGELOG and
  `build/tests.json`, the whole suite, no test lost and the task's own check on a fresh export. Green moves `main` in
  `/srv/awb-build/main.git` (a rebase first when `main` moved) and pushes it to GitHub; anything else is `hung`,
  `red`, `asked`, `stopped`, `stale` or `held` with one line, `result.md`, a board line and a mail. `awb build
  selftest` proves the planted cases and `awb seal check` runs it. The unit stays installed and not enabled.
- The build side gets its model key and the board's folder (self-build step SB3). `seal/setup.sh` copies the
  Workbench's model key to `/etc/awb/build-model.key` (yours alone, 400, copied again when the source changes;
  `build_model_key_source` in the host file names another key in one line), makes `tcp-shared/build` for the
  runner's board line, writes `build_push = yes` and gives the build settings `apiKeyHelper`. `sudo
  seal/verify.sh` checks the copy and the folder and runs `awb build selftest`.
- The deploy by task and the build line (self-build step SB4). `sudo awb deploy --task B-012` fetches `main` of
  `/srv/awb-build/main.git`, fast-forwards your `main` to it and deploys the commit the runner recorded as green
  for that task; it is refused while your tree has changes, your copy is not on `main` or has commits `main.git`
  lacks, for an unknown id and for a commit that is not on `main`. Every deploy now refuses a deployed commit your
  repository does not know and a target that does not descend from it (unless `--to` or `--rollback`), both with
  the line `git pull --rebase /srv/awb-build/main.git main`. `awb board` and the board of the portal (`build` of
  GET /api/board) print the build line from `<shared>/build/state.tsv`: the tasks, live, main and how many are not
  deployed, in fixed words only. `awb build status` ends with the same live, main and count, read fresh.
- The build user's selftest in the seal and the probe unit (self-build step SB5). `awb build probe` starts the network
  probes as the owner through the runner's own `systemd-run` form, with the address list, the hidden places and the
  environment of a task's session: the model endpoint and, with your login, `platform.claude.com` connect, a TCP API
  host and GitHub do not. `sudo seal/verify.sh` runs it and the runner's selftest; `awb seal check` runs the selftest
  and reads the record of the last probe. The selftest fails when one of its planted cases is taken out. Every task
  unit names the oneshot `awb-task-failed@.service` in `OnFailure=`: a task unit that fails while no runner waits on
  it puts one line on the board. The README names the limits the threat model accepts.
- Every draft for others passes the voice checks before he sees it (the work profile, proposal 4). `awb write
  check` blocks in mode mail and doc what he struck out most: "we", "our" and "wir" for "I", the jargon gotchas,
  quirk, pilot light and whole trick (cadence, estate, verbatim, granular and sharp edges were banned already), a
  paragraph with a bold lead, an invented name for an option, admissions of weakness, sales tone, promises in his
  name and the history of the draft; it reports a section the request did not ask for by its heading (`--request
  FILE`).
- The stop hook of a working session holds a reply that shows him a draft (a file under deliverables/ or a mail
  draft) before `awb write check` of that file ran clean in the same turn, with one line naming the file. A mail
  written into the reply must go into a file first; a Russian draft is exempt; the gate fails closed.
- `awb write translate FILE --to de --to ru` makes the German version and his Russian reading copy from the
  accepted text in one step, keeps every number, identifier and code block (refused on a difference) and runs the
  writing check on the German text. It reaches a model through the Ask path only; a working session cannot read
  that key, so the command writes the prompt file and stops with one line, and `--verify` proves the session's
  translation.

- The knowledge leaves as a dataset. `awb dataset build` writes the folder `tcp-facts-<date>/` and its zip: every
  fact on one line grouped by topic, one file per topic, the export with its metadata, the orderable services, the
  price list of each region as CSV, a one-line prompt in `PROMPT.md` and `HOW-TO.pdf`, each text file opening
  with the rules for the assistant that reads it and a best-before date. Its price lists carry only services the
  service description offers: rows of withdrawn services and of the hand-kept not-offered list are left out and named
  in the CSV header. The build checks itself: a fact that presents a service the service description does not
  offer stays out and is named, and a few price rows per region are fetched again from the live API, each check
  proved on a planted wrong case first. `awb refresh` builds it at the end of a clean run, `awb dataset put` sends it
  into the bucket.
- The dataset answers as an MCP server. Every build carries `tcp_facts_mcp.py`, a read-only server any MCP client
  (Claude Desktop, Claude Code, VS Code) runs next to the files: `tcp_facts_search`, `tcp_service_check`,
  `tcp_price_find` (the snapshot, the live price API with `--live`) and `tcp_calculate`, each answer with the dataset
  date, the best-before date and the rule. It reads nothing outside its folder and writes nothing. `services.md`
  now lists every short name of a service, the date from which one is no longer bookable and the services that are
  not offered. The build scans the server as it scans the other files, and `awb dataset put --github` puts it into
  the repository root.
- The release of `awb dataset put --github` lists the sha256 of the zip, `HOW-TO.pdf` and `MANIFEST.json` in its
  notes, one line each in `sha256sum -c` form, so a recipient can check a download. A `HOW-TO.pdf` that differs
  from its sum in the manifest refuses the release.
- One deploy command. `sudo awb deploy` brings a commit live as a release of its own, restarts only the services the
  change touched, brings the rules of every project up to date and ends with the status of each service.
  `--dry-run`, `--rollback`, `--to COMMIT`, `--only UNIT...` and `awb deploy status`.
- The vault daemon and the key service survive a code update. `awb vault reload` and `awb keys reload` hand the
  running service to the new code without a lock, so the name check stays unlocked and the keys stay loaded.
- The board for management. `awb board show` and `awb board write` give the status of every active project with its
  time and lag, what it waits on, its open items, its deliverables and its live resources. The console shows the same.
- Offered services come from the latest service description alone: `awb service check`, `list` and `update`, the
  list in both chats, a note under an answer that names a service not offered and a finding in the review.
- `awb projects sync` brings the template lines of every project up to date. A running session hears once when its
  rules changed.
- A project's status stays current. `STATE.md` opens with `Status:` and `Next:`, a session cannot end a reply while
  its commits are newer than that status, the console shows the status with its lag.
- Sessions ask less. At most one question per reply, at its end, with the default the session takes. Every other
  open decision goes into `OPEN.md`. `awb report --by questions` counts the replies that ended with a question.
- A plain `git commit` works in every project. RESOURCES.md lists each cloud resource with its handle and its full
  platform id, so a cleanup can address it exactly, and `awb cloud sweep` marks the resources a project lists.
- One command publishes the console page. `sudo awb web publish --from-queue ID` checks a finished UI run and puts
  it live with a backup, `--rollback` puts the old page back and `awb web status` shows what is live.
- The UI queue reports back. `awb ui submit` checks a UI task and hands it to Codex, and a result note with the
  validation, the summary and the publish command appears in `presentations/ui-tasks/` when the run ends, without
  anyone asking. `awb ui status` lists the runs and `--mail` sends a mail when one ends.
- The owner host lists the UI runs. `GET /api/ui-runs` answers id, state, creation and finish time, validation
  and summary of each run, newest first. A summary the name check finds something in is withheld with its class
  only, a file name in it shows as [file], and a session of the main host gets 403.
- Terraform and code files go through the exchange. `awb xchg put` and `awb check` read a dotted reference such as
  `module.lb.listener_port` as code, not as a web address, and still find names and mails. A session cleans its own
  results out of the lab bucket with `awb xchg clean`, so the owner no longer needs the console for it.
- A new test tenant in one command. After one `pass insert` of its admin login, `awb tenant setup ALIAS
  --domain-id ID` makes the read user and its key, registers the tenant and loads it into the key service, so
  architects can use it right away; `--lab-key` adds the lab key and `--alerts` the alert on a new access key.
- Fewer commands to copy. The owner's session runs the work-side commands that carry no name and change nothing in
  the cloud itself. `awb projects open CODE` starts a project session in the current terminal and a project session
  hands an architect at most one command block per reply.
- Pasted terminal output and background agent results reach the session. Addresses and web addresses in them no
  longer stop the prompt, while names, mails and secrets still do, and `awb paste` puts a long output into the
  project with its addresses masked.

- `awb import CODE` brings customer material into a project in one command, from the command line, the project's
  bucket folder or the vault inbox. It asks once for a new customer's names on your terminal and prints counts only.
  `--redo` runs the last import again after `awb register keep` repaired a wiped term. The project's session hears
  of the new copies at its next prompt. `awb words update` rebuilds the word lists from the public mirrors.
- The red-team pack of wipe mode: 405 invented cases in mails, chats, decks, code, tables, German and Russian text
  and typical TCP texts, run against the intake as built, with what is left written down.
- The wipe mode is measured on every refresh. `awb refresh` runs the red-team pack through the installed intake
  after the knowledge part and writes its leaks and losses per case module into the report and into
  `refresh/wipe.tsv`. More leaks or losses than the last run is overdue, and a pack that cannot show a planted leak
  records nothing. `awb board show` and the board page carry the line `wipe: L leaks, M losses of N (date)`.
- The word lists that keep a text readable when the intake wipes names: the capitalised words and two-word phrases
  of the public TCP documentation and service description, German and Russian words of tender and architecture
  texts, first names, the products of the usual source platforms and vendors, standards, German function words and
  the jargon of migration and contract texts. The partner's own company names are never taken for another company.
- The period report. `awb report --period --from 2026-04-01 --to 2026-09-30` lists every project with activity in
  the period (ledger entries, commits, STATE.md changes) in three blocks: active customer work, finished customer
  work and technical questions. Per project the customer code, kind, first and last activity, goal, ledger kinds,
  technologies, deliverables by type, whether a test tenant was used and the status line, with a summary above and
  the block rules at the end. `--format html` for a page. `--reveal` puts the customer names back, for the owner
  only, in his own terminal or into a file of his own outside every git working copy.

### Changed

- The build tasks run on your subscription login (self-build SB3, D-S7 changed). `build_model = login` is the new
  default of `/etc/awb/paths.conf`: you log the build user in once with `sudo -u awb-build -H
  /opt/claude-build/bin/claude login`, the runner copies that login into each task's home (600) and removes it with
  the home, a token the task refreshed goes back, and the login never lands in `runs/`, a log, the board or a mail.
  Without a login a task ends at once as `held: login missing` with that one command and the queue waits for `awb
  build retry ID`. `seal/setup.sh` copies no model key then, removes a copy left over and installs the build
  settings without `apiKeyHelper`; `build_model = key` brings the key path of SB3 back in one line. `sudo
  seal/verify.sh` names the mode and, with login, whether a login is there.
- The README of the public dataset comes from the build. The build's README.md and the how-to (PROMPT.md and
  HOW-TO.pdf) carry the section "MCP clients" after the warning, with the configurations for Claude Desktop,
  Claude Code and VS Code and a line that points at LICENSE. `awb dataset put --github` now copies README.md
  into the root of the repository, so every put replaces the root README with the build's; LICENSE stays
  untouched.
- The dataset TCP Facts warns before use with a public model. The section "Before you use it with a public model"
  (technical questions only, no customer data, a described setup can identify a customer, the use is the user's
  responsibility) is in the build README, `PROMPT.md` and `HOW-TO.pdf`, and the rules at the top of `facts.md`, the
  topic files and `services.md` gained the line "Technical questions only; no customer data in your prompts (see
  README)".
- The dataset TCP Facts names no person. The README, `HOW-TO.pdf`, its `/Author` metadata and the manifest field
  `compiled_by` (was `author`) credit `TCP Facts, github.com/shimoza/tcp-facts`, the dataset and its address, and
  carry the line "Compiled from public sources and live checks. Licence CC BY 4.0: name the dataset and its address
  when you reuse it. No warranty: the service description and the price list of the provider are the binding
  documents." `AWB_DATASET_AUTHOR` and `dataset_author` are gone.
- The dataset has a public home. `awb dataset put --github` puts its files into the root of github.com/shimoza/tcp-facts
  (raw links work), commits them as the GitHub account, tags the date and makes a release of the tag with the zip,
  `HOW-TO.pdf` and `MANIFEST.json`, the counts as notes. Before the commit the files are scanned for the owner's
  commit identity, an old author setting and a registered name; a hit refuses the put and nothing is published.

- The intake no longer stops for a name it does not know. Every name its rules recognise becomes a token of its
  class, numbered within the run (`[person 1]`, `[company 1]`, `[place 1]`, `[name 1]`). The other shapes of a wiped
  name are found again in the rest of the run: alone, inside a host name, with German or Russian endings. The private
  report lists every wiped value with its class and rule and the forms the run learned. The public report counts the
  rules that fired. `--force` is gone and `--review` stops once for the rare name that must keep a code. The owner
  inbox take, the bucket pull and the console's materials import hold no file for a name any more. The review opens
  vim when no editor is set.
- The work rules and the README name `awb import` and what the intake promises now: every name its rules recognise
  becomes a token, not every name. A held take is one line with the command to run.
- `awb register new` issues customer and partner codes only: person, company and place codes come from the review
  of `--review`. `awb vault show` without a path shows the newest private report of `--customer` or of the only
  customer with a report of the last day.
- `awb inbox take` finds the file you describe in your own words (the same letters, a kind of file, the newest, a
  part of the name). None or several matches list both inboxes. `--id` and `--all`.
- The harvest also asks after Terraform runs, the openstack client and plain calls to the TCP API, besides the
  Workbench's own calls.
- The push check lets the owner's own commit identity pass in his own repository.

### Fixed

- The plan of the seal no longer depends on the host it is printed on. `seal/setup.sh --dry-run` reads
  `AWB_SETUP_BUILD_HOST=fresh|present` (with `--dry-run` only): the probes of the build side (the build user, its
  groups and the suite's venv) then answer missing or present instead of looking at the host. The seal tests plan
  against a fresh host, so they pass on the sealed host too. `seal/verify.sh` no longer fails the build template on
  its own comment "no safe.directory": it looks for a `[safe]` section or a `directory =` line.
- A test that blocks again after its time limit, or that lost its alarm, fails within the limit instead of holding the
  whole run (a full run hung nine hours on 2026-10-10). After the limit the alarm repeats every second while the test
  runs, a thread ends the run with the stacks of every thread 30 seconds later when the test never comes back to Python,
  and an `awb hook` run inside a test gives the test's alarm back.
- Two test runs at the same time no longer wait on each other. Every test gets a vault, knowledge base, mirrors,
  check, admin and key sockets of its own run instead of the host's, the price API and the mirror sources point at a
  refused local address, and two tests that called `monkeypatch.undo()` (which dropped that setup and reached the
  owner's own register and check socket) undo only their own patches. A test fails after 120 seconds
  (pytest-timeout, now in the `test` extra of pyproject.toml) instead of hanging. The full run takes about 13
  minutes instead of 22: the dispatch test of `awb refresh` runs one wipe case instead of all 405 and does not wait
  between retries.
- A package upgrade no longer locks the Workbench. The project chat says when the name check is locked instead of
  calling the project unavailable.
- The deploy no longer reports a restarted socket unit as failed.
- The deploy no longer reports a socket-activated service as failed when only its code changed: such a service
  stays inactive until its first call, so the deploy does not wait for it.
- The deploy no longer ends in "awb-web.socket gave no answer" and exit 1 while the gateway serves: the front
  socket only the tunnel may open is proven through the gateway's status socket, its mode and group unchanged.
- The deploy names "next: sudo awb deploy --only" for a daemon on an older release only when a module it imports
  changed since that release, else the status says "its code unchanged since".
- `awb web status` without sudo says the sign-in log needs sudo instead of stopping with an error.
- A calendar invite whose lines are folded is read whole before names are looked for. A flavor id such as
  `rds.pg.c6.large.4` is no longer taken for a web address and the number at the end of a host name next to an IP
  address is no longer taken for a phone number.
- A project without a customer takes material in. `awb import CODE` runs for it without `--customer`: every name
  becomes a token, nothing becomes a code and the copies go out under the project code. `awb inbox take` takes such
  a project's files from its own bucket folder `in/`. `--customer new` still issues a customer, and an empty line at
  its forms prompt issues the code without a form. A take or an import refused in a session says only `held: run
  awb import tcp-xxxx as the owner in your own terminal`.
- Every project has its bucket folder from the start. `awb spawn` ends with `<YYYY-MM>/<code>/` with `in/` and
  `out/` in the owner bucket (the console's creation runs the same spawn), made by the key service (new op
  `owner_folder`; the work user never holds the bucket key). When the key service cannot be reached the spawn says so
  in one line and goes on: the first take or `awb bucket folder CODE` makes the folder. `awb inbox take` lists the
  new files of `in/` for a project with a customer too. "take the files from in" takes all of them.
- The owner page of the console puts files into a project's folder `in/` (`POST
  /api/projects/{code}/materials/upload`, owner host only, at most 25 MB; only the extension travels, never the file
  name; key service op `web_put_in` for the owner's processes alone). The page says the session takes them with
  "take the files from in". Its source browser and import button are gone.
- The work rules say that material lies in the project's `in/`, that no customer is normal and that a text he wrote
  and pastes is his brief. The hold line is for an unreadable file of `in/` alone. The sentence about
  `inbox/CUST-XXXX/` is gone from the rules, the README and COMMANDS.md.
- A build task's unit can resolve names again. When `/etc/resolv.conf` was a link to systemd-resolved's stub file,
  the runner read no nameserver and every task unit and the probe unit got an allow list without the resolver, so
  no unit reached the model endpoint. The runner now follows a system link under `/etc` or `/run` (root, a system
  account or the owner, no foreign write bit) and holds the queue with `no resolver for the task unit` when none is
  found. `awb build probe` counts a refused TCP API host or GitHub only after the same run reached the model
  endpoint (else "not proven"), and a failed line names the probe's exit: no name resolution or no connection.
- The suite of a build task no longer hangs in its unit (B-001 ended `hung: suite` at 16 % after 30 minutes). The
  suite unit allowed no address at all, so every test that talks to its own stand-in server on 127.0.0.1 waited for
  a timeout. It now has a network namespace of its own with its loopback alone (`PrivateNetwork=yes`,
  `IPAddressAllow=localhost`): no name resolves, the outside and the host's own listeners stay out of reach.
  `awb refresh` in the tests no longer fetches the real service description page (`mirror.sync_sd` read a default
  bound at import). The suite's helper interpreter writes no bytecode into the export, and the tests that need a
  work-side hook process or take the runner for the owner are skipped when the suite runs as the build user.

### Security

- An egress filter for the work user. An nftables table by uid lets a session reach loopback, the model API, the
  client's login, update and telemetry hosts and the TCP price API (`seal/egress/ALLOW.md`, each with its reason and
  how it was measured) and counts every other new connection by class. It ships in the mode count and refuses
  nothing: `awb egress status`, `awb seal check` and `awb deploy status` show the counts of the last 24 hours, and
  `sudo awb egress enforce` turns it to refusing only when those 24 hours counted nothing unexpected (`--accept
  CLASS` for a class you expect to lose); `sudo awb egress count` goes back. The test tenants stay reachable through
  the key service, which runs under the owner's uid and passes untouched; `seal/verify.sh` proves that with a
  planted connection of each uid.
- The gateway listens on the front socket alone: `awb-web.service` no longer passes `--ports 8080 8081` and the
  tunnel fence lets the tunnel's user reach no loopback port but the resolver (53), now that every ingress rule (the
  site, `/ask` and the owner host) points at the front socket (finding 12 of the review of 2026-10-10). `--ports`
  stays in the gateway for the tests; no installed file uses it.
- The name check reads every letter of another script that Unicode lists as confusable with one Latin letter as that
  letter, Lisu capitals and Cherokee small letters included, also inside a word of mixed scripts (finding 17 of the
  review of 2026-10-10). The table of 417 letters is generated once from the confusables data of UTS #39 and kept in
  the repository (`python -m awb.confusables confusables.txt` builds it again); symbols, digits and marks stay out,
  so a box border is no letter.
- `/kb`, `kb_find` of Ask and every search withhold an entry that carries what `awb kb add` refuses, not only a
  registered name: a mail address or other structured data, a register or project code, an identifier, a home path,
  a secret or a token (P3 of the review of 2026-10-10). An entry file written past `kb add` is no longer shown. A
  source web address alone still passes.
- The mail of a put names the file by its id alone, never by a name the session chose (finding 18 of the review of
  2026-10-10). Every field of an exchange mail is printable ASCII, so a session can no longer write a line separator,
  a bidi control or a paragraph of its own into a mail you trust.
- The owner can narrow every leased key with `tf_services` in `keys.conf` (finding 16 of the review of 2026-10-10).
  The unlock used to refuse the line as an unknown setting, so the default list with OBS always applied. A shorter
  list is what the lease allows, an empty line allows no service, and `iam` or a name that is no service stops the
  unlock with the line number.
- A person written with the English possessive (`<First> <Last>'s notes`, also with a typographic apostrophe) is
  still found by the short-text rule of the commit gate and the ledger (finding 7 of the review of 2026-10-10). A
  trailing `'s` or `’s` is no part of a name word.
- The tenant view with the IAM domain names is no longer served on the loopback port 8183 (finding 14 of the review
  of 2026-10-10). The tenant service answers it on an owner socket of its own (root, group awb-web, 0660) and only
  to the gateway's uid; the port keeps the reader view. The owner host refuses every request whose
  `Sec-Fetch-Site` is there and not `same-origin` or `none`, GETs and the sign-in page included, so a page of the
  main host can no longer send the owner cookie along (finding 15).
- A restart of the gateway keeps every running wait of the sign-in, also after a flood of refused tries (finding 13
  of the review of 2026-10-10). The waits live in a small state file next to the sign-in log, and a refused try
  writes one line per address and minute, the rest of the minute as one counted line, so cheap tries no longer push
  the failures out of the log. `sudo awb web status` counts every try.
- The gateway's ports 8080 and 8081, kept until the tunnel points at the front socket, answer the tunnel's user
  alone, and the gateway takes the client address from `CF-Connecting-IP` only there and on the front socket
  (finding 12 of the review of 2026-10-10). A local process can no longer pick a new address key for each try, nor
  reach the sign-in through those ports at all.
- A stranger can no longer keep the owner off the owner host (finding 4 of the review of 2026-10-10). A failure
  there delays the next try of that name from that address only, so one wrong password a minute from elsewhere no
  longer holds the owner's right password and code back. A wrong code after the right password still delays the
  name from every address, so whoever holds the password guesses at most 60 codes an hour.
- The main host answers the owner's login name exactly like an unknown name, the wait after five failures and the
  site cap included, so the name is no longer given away (finding 10 of the review of 2026-10-10). The owner host
  keeps its own sign-in, which never locks. A try for a login is counted before its password check, so guesses sent
  at once from many addresses stop at the fifth (finding 11).
- No tool call of a session reaches the session records in `<shared>/sessions` (finding 23 and P5 of the review of
  2026-10-10). A running session can no longer read the ids of refused sessions in `receipts.tsv`, plant their pass mark
  or rewrite `guard.log`. A search over the whole shared folder is refused for the same reason; its outbox and datasets
  stay open.
- The tool guard takes Monitor, the client's second tool that runs a shell command (finding 9 of the review of
  2026-10-10). The work user's settings now deny, next to the connectors, every tool of the client that reads or sends a
  file, runs a command or starts work elsewhere and that the guard does not see: PowerShell, LSP, the artifact and
  worktree tools, Workflow, RemoteTrigger, SendUserFile and the MCP resource tools.
- A relative Glob or Grep pattern is taken under the folder of the search and through its links, so `../<project>/*.md`
  is refused (P4 of the review of 2026-10-10). A `..` after a glob character is refused.
- A relative path of a shell command is taken against the folder the command stands in, through its links (findings 20
  and 22 of the review of 2026-10-10). A bare `cd` leads to the home and `cd X` to X, so `cd && cat .ssh/...` is
  refused, and so is a link inside the project that points out (`cat peek/STATE.md`). A link is resolved before a `..`
  after it climbs. A `cd` the guard cannot follow (`cd -`, a variable, CDPATH) is refused, and the string of `bash -c`
  and of `eval` is read as a command line too.
- The tool guard reads a shell command the way the shell splits it (findings 8 and 21 of the review of 2026-10-10). A
  quote or a backslash inside a path (`tc''p-`, `tcp\-`) no longer splits it past the guard, an option with a path glued
  to it (`tar -C/...`, `curl -d@...`) is read from where the path begins, and `$PWD`, `${PWD}` and `~+` stand for the
  session folder, so `$PWD/../<project>` is refused. A quote or an escape inside a word that carries a path, any other
  variable or substitution inside a path, `~-` and a quote that never closes are refused as a path the guard cannot
  read.
- A take from the owner inbox checks the file before it moves it (findings 6 and 19 of the review of 2026-10-10). A
  file in a folder that is not exactly one customer code (`cust-zz22`, `CUST-ZZ22-old`, a deeper level) is no
  longer read as a file of the top and stays in the inbox, and so does a file above `max_mb` of keys.conf; the take
  fetches the version its listing showed.
- A session puts nowhere but `<project>/from-session/<date>/<file id>` of the lab bucket (finding 3 of the review of
  2026-10-10). A raw put into the project's `in/` or under any other key is refused before its bytes are received,
  and every put meets the name check and the send gate of the key service.
- The take by words is no oracle for hidden names, and a project the owner never granted has no customer
  (findings 2 and 5 of the review of 2026-10-10). Words meet only the names a session may see: a file of the owner
  inbox or of `in/` is found by its kind, as the newest, with `--all` or by its id, never by a word of its name. A
  session can no longer ask whether a name is in the owner inbox or take a file there by name. Without the owner's
  grant a project has no customer for the key service: its owner-inbox take holds, its take from `in/` runs under the
  project code and its put meets the strict send gate. Run `awb projects kind CODE query` once for a customer project
  before its first take from the owner inbox.
- A leased key never reaches the exchange buckets (finding 1 of the review of 2026-10-10). The key service refuses
  a lease of the bucket tenant's lab key, under any alias, and every lease denies every action on the lab bucket and
  the owner bucket. OBS is allowed only on buckets whose name starts with the project code, and a lease without a
  project gets no OBS. `awb cloud lease-check` therefore runs on a tenant other than the bucket tenant and leaves OBS
  unchecked.
- The key service takes a project's customer from the owner's grant, never from the work user's projects.tsv
  (the residual of F5 and F6). `awb projects kind CODE KIND` run by the owner records the row's customer in the
  grant. The owner-inbox take, the project take, the strict form of the send gate and the lease refuse a row that
  names another customer, in one line with the project code. A grant of before 2026-10-10 is filled by one re-run
  of `awb projects kind`; `awb projects check` as the owner lists those and every row that differs.
- A project session stays inside its project. `awb hook pre-tool` guards Read, Edit, MultiEdit, NotebookEdit,
  Write, Glob, Grep and Bash in the work user's settings: a call that reaches another project, the client's folder
  or a key folder (`.ssh`, `.config`) is refused with one line that names the class of the place, never the path.
  The own project, the shared folder, the knowledge base, `/tmp` and the installed code stay open. A session
  without a project folder is refused on a sealed host. Every refusal is logged and the stop hook counts them per
  project and day next to the questions count (`awb report --by questions`). `awb hook selftest` and `awb seal
  check` fail when a planted refusal passes.
- A session on a sealed host starts only with every guard proven active. The start hook checks that the managed
  settings carry the hooks of the release and run the installed command, that the project's repository carries the
  commit gate, that the vault daemon answers a check and the key service a ping, that the project's rules are the
  installed ones and that the host mode is known. The context opens with `guards: 6 of 6 active (<release>)`. A
  missing guard makes the context one line that names it (never a path), and every prompt is refused with the same
  line until it is fixed. An owner session gets the line without the project check and is not refused. `awb board
  show` and `/health` carry the count of sessions refused at start today.
- A locked vault blocks every prompt of a work session instead of warning, with the time of the lock. A session
  starts without its project files while the name check cannot run.
- The installed `awb` command runs the interpreter isolated, so a package planted in a user's own folders is never
  loaded ahead of the installed code.
- Both daemons refuse to be inspected by other processes of the same user.
- The console's customer list shows codes only and the console no longer registers customers: no customer name
  crosses Cloudflare to the browser. New customers are registered with `awb register add` on the owner's terminal.
- Every console user signs in with a login of their own. `sudo awb web user add|reset|remove LOGIN` makes a random
  password of 20 characters, shows it once and keeps only its digest. A login waits 15 minutes after 5 failures
  within 15 minutes, twice as long after each further series up to 24 hours, and the whole site pauses sign-in after
  30 failures within an hour. Every attempt is logged without the password, and `sudo awb web status` shows the
  last day and every login that waits.
- The console's front door is a Unix socket only the tunnel's own user may open: the gateway checks the peer's uid
  before it reads a byte, cloudflared runs as its own user, and a firewall rule keeps that user off every local TCP
  port, so neither a local process nor a changed tunnel route reaches the gateway any other way. One address (an
  IPv6 /64) gets one sign-in at a time, a busy sign-in answers "try again" without counting, a sign-in form works
  once, a login keeps five sessions and a full table refuses instead of evicting. An owner login is never locked out,
  it only waits up to a minute after a failure, and it gets a TOTP key for the owner level
  (`sudo awb web user add LOGIN --level owner`). `sudo awb web host HOST` names the owner level's host. A reset
  says on its first line that the old password and phone key no longer work.
- The console has an owner level on a host name of its own. The owner signs in there with his password and a code
  from his phone; the session ends after 15 idle minutes and after an hour. The owner page is a fixed file of the
  release and shows the vault and key service state, the UI runs, the intake counts, the customers, new projects,
  the materials and the tenants, codes only and never a command to copy. Customers, project creation and imports
  are no longer on the main site. Behind the owner routes a service of its own user reads one status file that
  `awb owner status --write` writes every minute. File names of the inbox show as ids with their type, and readers
  see a tenant's account number instead of its domain name.
- Publish from the owner page. The newest run of the UI queue that passed its checks goes live with one click and a
  fresh code from the owner's phone; the code is checked twice, by the front door and by the owner service, and a
  small root service publishes only the newest passing run, never an older one or the same page again, and keeps the
  last 10 pages. `sudo awb web publish --from-queue ID --newest` does the same in the terminal.
- A session can no longer give its own project access to a test tenant. The key service takes the project's kind
  from the owner's grant, which `awb projects kind CODE project` writes when the owner runs it in his own terminal,
  and from the project's row only to take access away.
- The key service can lend a session a temporary key: for at most an hour, for the services of a Terraform set only,
  never for identities, and only for a project the owner granted. `awb cloud lease-check ALIAS --yes` checks once on
  a test tenant that such a key works for every service, creating and deleting one small resource each.
- The lab key can be a user of its own. `awb tenant setup ALIAS --lab-user` makes awb-lab-<number> with full rights
  on the compute, network, load balancer, NAT, DNS and image services of the lab project only and on the storage of
  the account, without any right on identities, and puts its key in place of the admin's.
- The routine upgrade no longer updates cloudflared and Terraform: the seal and every deploy hold both packages, so
  an upgrade of either is a deliberate step.
- The exchange names no file. A result goes into the bucket under an id, its name is checked also for a picture and
  for the name given with `--as`, and the mails and the log of the exchange carry only codes, ids, sizes and states.
- Ledger texts leak nothing and are not lost. A ledger text with a name registered after it was written is withheld
  from the career candidates, and a session hears once when the draft of its commits was refused and adds the entry
  itself.
- The knowledge page and the questions page check every fact against today's register, so a fact with a name
  registered later is withheld, and the knowledge base refuses a commit that skips the checks of `awb kb add`.
- A commit message or a ledger line that reads like a name the register does not know is refused with its line and
  how to reword it; a technical term goes into `rules/allowed-terms.txt`. Also: the harvest reminder asks for a
  source the knowledge base takes, a foreign tag value on a test tenant shows as `-`, a project's first commit runs
  the commit gate, a pdf or a sheet from the lab inbox stays out of git, and the first ledger draft of a day no
  longer misses commits.
- A take from the owner inbox goes to the project's own customer. A project without a customer takes a file only
  from a customer's folder of the owner inbox (`inbox/CUST-XXXX/`), and a code the session names only has to agree.
- The name check of a take from the lab inbox and the checks of `awb xchg put` (the name check and the send gate)
  run in the key service too, so a changed session cannot skip them. A session can no longer fetch, copy or delete
  a lab inbox file except through that checked take.
- The key service decides on a put before it receives the bytes, so a refused put stores nothing.
- The customer socket of the console answers only the web user, and the work user is not in its group. Project
  creation counts a customer by its outbox folder, as `awb spawn` does.

## Week 40, 2026-09-28 to 2026-10-04

### Added

- The public edition of the Workbench (2026-09-28), under the Apache License 2.0.
- A read-only portal over the knowledge, live prices, the projects and the reviews.
- The Ask page: a question in plain words, answered only from the checked sources, name checked before it leaves,
  with a daily budget.
- The Workbench as a plugin for Claude Code, with this repository as its marketplace. Version 0.2.0 adds the
  exchange skill, the migration skill and its workers.
- `awb calc`: exact decimal arithmetic, every result recorded. The review takes a computed number only with its
  recorded calculation.
- `awb migrate`: Azure to TCP in phases (discover, map, estimate, plan, review). Each machine gets the nearest TCP
  flavor that is not smaller, priced live.
- The key service. Keys, logins and passwords of the test tenants stay on the owner's side. Sessions call through it
  and never see a value.
- Daily snapshots of the test tenants: what ran where, when and for which project.
- The harvest: a session that called the TCP API adds what it learned live to the knowledge base.
- The exchange: two inboxes and a folder per project in object storage. Sessions take files only through the key
  service, an original only through the owner's intake.
- Two kinds of work: a query (knowledge base, docs, prices) and a project (also the test tenants).
  `awb projects kind` switches between them.
- `awb kb export`: the checked facts as a JSON Lines dataset.
- `awb projects delete`, `awb projects list` with the goal of each project and `awb vault show` for the private
  intake report.
- `awb register review`: the candidates of a blocked intake sorted in one editor pass.
- The web console in the repository: its API contract (`docs/api/openapi.yaml`, `awb api check`), the handlers and
  their tests. The project chat reads the sanitised inputs of a project and searches long ones.
- The README for architects.

### Changed

- Calls to the TCP API keep a pause per host and wait what an HTTP 429 names. `awb cloud usage` reports the calls.
- Registering a customer opens its outbox folder, so a project can start before the first intake.

### Security

- The key service refuses every write to the identity service and keeps one log line per call.
- The console reads a test tenant without a project only through its own read-only service user.
- Knowledge search checks for names in halves, so a broad search never hits the rate limit of the name check.
- No test writes to the owner's home folder.
