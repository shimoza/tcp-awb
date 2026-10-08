# Changelog

What changed in the Architect Workbench, one entry per week (ISO week, Monday to Sunday), newest first. The form
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Customers and projects appear as codes only.

## Week 41, 2026-10-05 to 2026-10-11 (in progress)

### Added

- The knowledge leaves as a dataset. `awb dataset build` writes the folder `tcp-facts-<date>/` and its zip: every
  fact on one line grouped by topic, one file per topic, the export with its metadata, the orderable services, the
  price list of each region as CSV, a one-line prompt in `PROMPT.md` and `HOW-TO.pdf`, each text file opening
  with the rules for the assistant that reads it and a best-before date. `awb refresh` builds it at the end of a
  clean run, `awb dataset put` sends it into the bucket.
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

### Changed

- `awb inbox take` finds the file you describe in your own words (the same letters, a kind of file, the newest, a
  part of the name). None or several matches list both inboxes. `--id` and `--all`.
- The harvest also asks after Terraform runs, the openstack client and plain calls to the TCP API, besides the
  Workbench's own calls.
- The push check lets the owner's own commit identity pass in his own repository.

### Fixed

- A package upgrade no longer locks the Workbench. The project chat says when the name check is locked instead of
  calling the project unavailable.
- The deploy no longer reports a restarted socket unit as failed.

### Security

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
