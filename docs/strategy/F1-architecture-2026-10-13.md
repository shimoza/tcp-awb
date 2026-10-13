# F1: architecture review of the Workbench and a plan to make it simpler

Date: 2026-10-13, commit 8a0f44c. An analysis, not a code change. Line numbers are those of that commit.

## How this was done and what it could not see

Read: the five documents named in the task, `seal/web/README.md`, `docs/review-2026-10-13.md`, the 62,749 lines
under `awb/`, the units under `seal/` and a sample of the 106 test files. Run: the suite once, in a throw-away venv
on Python 3.12 in a cloud container, as root, with `--durations`.

Limits of the code alone:

- The console page `architect-workbench.html` is not in the repository (`awb/tcp/web/publish.py:62` reads it from
  the owner's home). I cannot say which old portal pages the console still links to.
- The container has no systemd, no nftables, no vault and runs as root. 50 tests of `test_seal.py`, `test_deploy.py`
  and `test_egress.py` fail as root instead of skipping. Nothing on the host side was run.
- The clone is shallow (109 commits, three days). How the modules grew is not visible.
- The timing is one run on one machine, stopped at 71 percent by the 50 failures. The vault tests, which the README
  names as the long ones, had not run yet.

## 1. Two paths that do the same job

**1a. The old portal and the console.** The gateway still forwards `/kb`, `/price`, `/projects`, `/reviews`,
`/tenants`, `/health` and `/portal` to `awb portal serve` (`awb/tcp/web/gateway.py:34`, `:1080`); the host runs the
portal on 8180 for that alone (`seal/web/awb-portal.service.d/90-awb-web.conf`). Two pages exist a second time as
JSON routes with better checks: the project list (`awb/tcp/portal.py:148` against `awb/tcp/web/projects_api.py:124`,
which drops deleted projects and runs the name check) and the review counts (`portal.py:158` against
`projects_api.py:135`, which refuses symlinks). Three exist only in the portal: `/kb` (`portal.py:92`), `/price`
(`portal.py:130`) and the API usage lines of `/tenants` (`portal.py:193`). The portal's `/tenants` reads snapshot
files, the console's `/api/tenants` reads live (`tenant_api.py:217`): two answers to "what runs there".
Do: add `GET /api/kb`, `/api/price` and `/api/usage` to the reader service, move the three pages into the console
page, delete `portal.py`, its unit, its drop-in, the forward and `tests/test_portal.py`. Risk: medium, the page is
edited outside the repository through the UI queue. Effort: 10 to 14 h. Undone: one more process and unit in every
deploy and two project lists that can disagree in front of a reader.

**1b. Two Ask engines.** The model loop is written twice (`awb/tcp/ask.py:240`, `awb/tcp/web/chat_service.py:419`).
The question screen differs: `ask.py:208` runs `check.check_text` and no secret detectors, `chat_service.py:192`
runs `vault.check_remote` plus the gate's secret, private-key, token and homepath detectors. The budget reader
differs: `ask.py:179` reads an unreadable usage file as empty, `chat_service.py:279` fails closed. On the host only
`chat_service` runs (`seal/web/awb-ask.service.d/95-project-chat.conf`); `ask.ask()` and `ask.Handler.do_POST` are
reached by `awb ask QUESTION` and a bare `awb-ask.service` alone.
Do: `Conversations.ask_json` becomes the one engine; `awb ask QUESTION` and the form page go through it; `ask.py`
keeps the settings, the tools, the key file and the page template. Risk: low. Effort: 6 to 8 h. Undone: the CLI Ask
sends a question the web Ask would refuse.

**1c. Five text screens.** Besides 1b: `materials_api.py:322` (`check_text` plus four detectors),
`projects_api.py:47` (`vault.check_remote`, masks), `awb/tcp/owner.py:189` (`gate._scan_text` plus
`publish.name_matcher`). Each decides alone which classes refuse and which mask. Do: one `check.screen(text,
paths, classes, mask)`. Risk: low. Effort: 4 to 6 h. Undone: a new detector reaches some doors and not others.

**1d. Two doors to the owner bucket, two credentials, two limits.** The CLI side (`awb bucket`, `awb import`,
`awb dataset put`) opens `obs.Client` with `AWB_BUCKET_KEYS` or `bucket_keys` (`awb/bucket.py:60`); the key service
opens it with the lab key of `bucket_tenant` (`awb/tcp/xchg.py:262`). The bucket name defaults twice (`bucket.py:59`,
`xchg.py:79`). The layout `<YYYY-MM>/<code>/in/` is built in `bucket.py:108`, parsed in `xchg.py:817` and listed in
`materials_api.py:191`, which takes every month while `bucket.find_folder` (`bucket.py:86`) refuses a code under two
months. The upload limit is 25 MiB in the gateway and the materials service (`gateway.py:31`,
`materials_api.py:41`) and `max_mb`, default 100, in the key service (`xchg.py:79`, `keys.py:468`).
Do: `bucket.py` owns the layout and the limits. Then decide whether the owner's CLI goes through the key
service too, which leaves one credential. Risk: low, then medium. Effort: 4 h, then 8 to 12 h. Undone: a folder one
side accepts and the other refuses, seen by the owner as a take that "finds nothing".

**1e. Two mail clients, two intake drivers.** `awb/tcp/ui.py:283` and `awb/buildrun.py:350` are the same `notify`
request. `awb/pastein.py:612-694` repeats the driver of `intake.run` through the private names `_Engine`,
`_body_hits`, `_public_text` and `_write_private_report`. Do: one `keys.send_mail`; one public `intake.run_text`.
Effort: 3 h. Undone: a change to the intake driver misses the paste door.

**1f. `awb intake` and `awb import`** both call `intake.run` (`awb/cli.py:147`, `awb/importcmd.py:241`). Make
`awb intake` an alias. 2 h.

## 2. Dead or nearly dead code

Very little. Every command, rules file and workflow has a reader. What remains:

- `jobs.wait_gone` (`awb/jobs.py:222`): no caller outside `tests/test_jobs.py:151`. Delete with `INTERFACES.md:911`.
- `--json` on the hidden `deploy plan` (`awb/deploy.py:2212`) is never read; `plan` always prints JSON.
- `awb/confusables.py` is a generator run by hand (`deploy.py:134`); nothing imports it, no test runs it. Move it to
  `tools/` outside the package, so the import map of the deploy stops carrying it.
- Host-file keys read and never written or documented: `admin_socket` (`awb/config.py:240`), `build_push_url`
  (`awb/buildrun.py:340`).
- Fifteen `AWB_*` variables documented nowhere: `AWB_ADMIN_SOCKET`, `AWB_CHECK_SOCKET`, `AWB_DATASET_AUTHOR`,
  `AWB_KEYS_ADMIN`, `AWB_KEYS_CONF`, `AWB_KEYS_SOCKET`, `AWB_OWNER_PUBLISH`, `AWB_PRICE_API`, `AWB_THROTTLE_DIR`,
  `AWB_UI_SOURCE`, `AWB_WEB_PUBLISH_STATE`, `AWB_WEB_ROOT`, `AWB_WEB_SIGNIN_LOG`, `AWB_WEB_STATUS_SOCKET`,
  `AWB_WEB_USERS`. Most exist for the tests. One table in `INTERFACES.md`.
- The usage text of `awb/cli.py:15-60` is stale: it leaves out `check`, `gate`, `write translate`,
  `kb bench|recheck|export`, `keys reload`, `vault reload|show`, `xchg clean`, `cloud sweep|usage|tenants|call`,
  `hook pre-tool`, `register review` and `projects kind|delete|open`; the `DELEGATED` help leaves out `kb index`,
  `kb verify` (`cli.py:579`) and `review calibrate` (`:588`). Point at `COMMANDS.md` or generate it from the parsers.
- `awb/tcp/inbox.py` is a two-line shim because `_delegate` wants `module.main` (`cli.py:623`).

Effort for all of it: 4 h, 8 h more for a generated `COMMANDS.md`. Undone: nothing breaks; a session that reads the
docstring gives the owner a wrong list of commands.

## 3. Modules to split or to join

- **`awb/buildrun.py` (3,535 lines) is five programs.** The unit builder (`SystemdUnits`, 367 to 647), the code
  that runs inside the build user's units (648 to 1015), the judge (`check_*`, 1088 to 1715), the probe (2851 to
  3058) and the selftest with its planted world (3081 to 3535, called from `awb/seal.py:205` alone). Split into
  `build_units`, `build_unit_side`, `build_checks`, `build_probe` and `build_selftest`. Effort: 6 to 8 h. Undone:
  every review of the runner (the one of 2026-10-13 had eight findings in it) reads 3,500 lines to judge 300.
- **`awb/hooks.py` (2,761 lines).** The tool guard and the build guard (760 to 1566) share only `project_root` with
  the hooks; the session receipt (2284 to 2626) is used by `projects.py` too; `client_settings` (189 to 291) only by
  `projects.py:489`. Split into `toolguard.py` and `receipt.py`. Effort: 4 to 6 h.
- **The intake is one pipeline in eleven files**: `intake`, `wipe` (3,176 lines), `pastein`, `candidates`, `planted`,
  `intake_counts`, `words`, `matcher`, `patterns`, `normalize`, `importcmd`. `pastein` reaches 22 private names of
  `wipe`, `intake` 8. Make a package `awb/intake/` with `engine`, `run`, `paste` (today's `pastein`), `review`
  (today's `candidates`) and `wipe/` by rule family (basic rules 86 to 653, lexicon 654 to 1024, proposed rules 1025
  to 2598, learned forms 2599 to 2849, run state 2850 to 3176). Rename `awb/paste.py` so it stops looking like
  `pastein`. Effort: 10 to 14 h. Undone: `gate.py:781` pulls the whole wipe module into every commit through
  `intake.short_text_candidates`. Every rule change is a 3,000-line context.
- **`awb/vault.py` (1,819 lines) carries the daemon toolkit of the key service.** `sd_notify`, `_Pair` and
  `hand_over` (707 to 893) are used 15 times from `awb/tcp/keys.py`. The client (404 to 605) is imported at about 60
  sites and makes the cycles `check` to `vault` and `register` to `vault`. Split into `handover.py` and
  `vault_client.py`. Effort: 4 h.
- **`awb/tcp/xchg.py` (1,310 lines) is two sides.** Lines 246 to 912 run inside the key service (`keys.py` imports
  them lazily at six places); 913 to 1310 are the session client. Move the service side to `keys_exchange.py`.
  Effort: 3 h.
- **Four project-root finders**: `hooks.project_root:507`, `review.find_project:1653`,
  `pricebook.find_project:1152`, `translate._project_root:90`; three `_require_register` (`hooks:373`, `kb:305`,
  `writing:1781`); three `_counts` (`hooks:353`, `review:230`, `kb:291`). One each. Effort: 3 h.
- **`awb/tcp/web/contract.py` (1,853 lines)** re-types every route, schema and error text; `tests/test_web_contract.py:269-275`
  pins the strings by hand. Backends export their texts. 6 h.
- **Lazy imports.** 343 indented `from awb` lines; 19 pairs exist only to break a cycle (among them `calc` with `review`,
  `hooks` with `projects`, `keys` with `tenants` and `owner` with `publish`). The splits above remove about eight.
  `deploy.import_map` (`deploy.py:414-663`) follows them to decide what to restart.
- **Leave**: `status.py` with `status_shape.py` (different uids by design), `paste.py` with `pastein.py`
  (different sides), `kb.py`, `lab.py`, `pricebook.py`, `tcp_facts_mcp.py` (standalone on purpose).

## 4. The same rule written twice

| rule | copies | differ | do |
|---|---|---|---|
| project code | `codes.py:30` says `[a-z2-7]{4}` and allows `hcs-`; 23 local spellings in 15 files, 11 of them `[a-z0-9]{4}` (`grants.py:25`, `keys.py:84`, `xchg.py:84`, `gateway.py:1044`, `projects_api.py:18`, `tenant_api.py:90`); `xchg.py` and `gateway.py` carry both | yes | one `codes.PROJECT_RE` and a test that greps the package |
| customer code | 7 copies of `CUST-[A-Z2-7]{4}`, none uses `codes.PLACEHOLDER_RE` | no | same |
| fact id | `kb.py:112` `[A-Z2-7]{4}`, `review.py:927` `[A-Z0-9]{4}`, `contract.py:899` any length | yes | same |
| calc id | `calc.py:40` `K-[1-9][0-9]{0,4}`, `migrate.py:698` `K-[0-9]+` | yes | same |
| hook list | `hooks.py:126`, `plugin/hooks/hooks.json`, `seal/work-claude/settings.json`, `managed-settings.json`; `tests/test_guard.py:202` pins them | no | generate the three files from `hooks.client_settings` |
| hook count | `README.md:505` says five hooks, `README.md:249` and the rest say six | yes | fix the line |
| owner routes | `gateway.py:796` and `owner_actions.py:53`; `/api/owner/tenants` outside both (`gateway.py:1255`) | no | one table |
| routes and contract | `awb api check` (`contract.py:1677`) checks the YAML against `build()`, never against `gateway.py` or the backends | n/a | a test that walks the gateway with every contract path |
| text suffixes | `xchg.py:91` (21), `materials_api.py:44` (14), `hooks.py:1849`, `gate.py:90` | yes | one list in `extract` |
| upload limit | 25 MiB twice, 100 MiB in the key service (1d) | yes | one constant |
| commands | `cli.py` docstring, `DELEGATED` help, `COMMANDS.md`, `README.md`, `INTERFACES.md` | yes | one source |

Effort for the table: 6 h. Undone: `projects.py:574` issues base32 only, so the wider regexes are harmless today
and a trap for the next alphabet.

## 5. What makes the suite slow or fragile

Measured: 2,650 of 3,731 tests in about 6.5 minutes as root. The 60 slowest took 163 s, 87 s of them in
`tests/test_build_runner.py` and 24 s in `tests/test_deploy.py`. The suite is not slow for its size. It is slow in a
few places and fragile in more.

Slow:

- The runner's selftest and probe build the planted world per test (`test_build_runner.py:617`, 12 s; the probe
  11 s; 13 parametrised cases of 2 to 3 s). `buildrun.World` (`buildrun.py:3288`) runs `git init`, `add`, `commit`
  and a bare origin for 144 uses.
- `test_deploy.py` copies `seal/` 16 times and `awb/` once (`:82-91`, `:164`) and runs `git init` in ten tests;
  `test_seal.py` copies `awb/` twice per layout (`:735-745`).
- Fixed sleeps: 24 in `test_vault.py` (about 10 s), `test_hooks_redteam.py:266` 2 s and `:281` 3 s,
  `test_ledger.py:171` 1.5 s. `cloud.py:183` sleeps `Retry-After` for real, up to 30 s, outside the `jobs.retry`
  hook that `conftest.py:135` disarms.

Fragile:

- **Root fails instead of skipping.** 50 tests assert on "refused as root" and "the owner must be a user, not root".
  The README says the egress tests skip as root; the seal and deploy tests do not.
- **Timing of real daemons.** `test_vault.py:630`, `:1143`, `:1249` and `test_keys.py:556` start `vault serve` and
  `keys serve` as processes and wait for `READY`; `test_vault.py:1206` asserts a wait "more than 4.5 s". The README
  admits `-n 2` fails on these (`README.md:212`). Two failures of this run were of that kind.
- **Process-wide patches at import.** `tests/conftest.py` replaces `shutil.copytree` (`:109`),
  `BaseServer.serve_forever` (`:121`), `jobs.retry` (`:141`) and `intake.selftest` (`:190`) for every test. The
  selftest cache keys on the caller's file name and an id tuple of ten modules (`:157-183`).
- **One shared folder for the run.** `getbasetemp() / "no-host"` (`conftest.py:392`) is `AWB_SHARED`, `AWB_VAULT`,
  `AWB_KB` and `AWB_THROTTLE_DIR` for every test without `home`; throttle stamps carry over (`throttle.py:28`).
  `config.is_work_user` is patched in 25 tests; then `config.paths()` falls back to the real `~/tcp-shared`, which
  only the audit-hook guard stops (T3H-a).
- **Host tools fail rather than skip**: gpg (no `which` anywhere), `pdftotext` (`test_dataset.py:143`), git
  (`test_gate.py:477`).
- **`build/tests.json` is not the suite.** 810 entries against 3,731 tests; 183 entries run a whole file;
  `tests/test_send_gate.py` and `tests/test_throttle.py` are in no entry.

Do: a module-scoped `world` for the runner tests (a git repository they only read), a session-scoped copy of `seal/`
and `awb/`, `pytest.skip` as root, a clock stub for the hand-over asserts, one temporary folder per test for the
four `AWB_*` roots, `cloud.py` through `jobs.sleep`. Risk: low to medium; the vault timing tests will argue. Effort:
10 to 14 h. Undone: the runner's whole-suite judge takes the full time per task. A loaded host turns a green task
red for reasons the owner reads by hand.

## The plan

Twelve steps, each one build task. "Code" needs no host. "Host" needs systemd, nftables or the vault on the
owner's side and his deploy.

1. **Code.** Section 2 without the generated docs. 4 h.
2. **Code.** One `codes.PROJECT_RE`, `CUST_RE`, `KB_RE`, `CALC_RE`; a test that greps the package for local
   spellings; the hook count line in the README. 4 h.
3. **Code.** One text screen in `check.py` for the five doors (1b, 1c), with a table of which door refuses and which
   masks. 6 h.
4. **Code.** One Ask engine (1b). 8 h.
5. **Code.** Section 5: root skips, one folder per test, module and session scope for the copies, `cloud.py`
   through `jobs`. Measure before and after with `--durations`. 10 h.
6. **Code.** `buildrun.py` into five modules, `hooks.py` into hooks, toolguard and receipt. No behaviour change.
   `deploy.import_map` must still find every edge: run `awb deploy --dry-run` on the host afterwards. 12 h.
7. **Code.** `vault_client`, `handover`, `keys_exchange`; one project-root finder; one `send_mail`. 8 h.
8. **Code.** The intake package with the wipe rules by family; `pastein` on a public `intake.run_text`. 12 h.
9. **Code.** One owner of the bucket layout, the suffix list and the upload limit (1d without the credential); one
   owner-route table; the gateway route test against the contract. 6 h.
10. **Host.** `/api/kb`, `/api/price`, `/api/usage` in the reader service, the three pages in the console through the
    UI queue, then the portal removed: its unit, its drop-in, `BACKEND_ROUTES` and the forward. Needs
    `sudo awb deploy`, `seal/web/install.sh` and a look at the console in the browser. 12 h.
11. **Host.** The owner's CLI reaches the bucket through the key service (1d, the credential): `awb bucket` and
    `awb dataset put` call the admin socket and `AWB_BUCKET_KEYS` goes. Needs the key service restarted and unlocked
    and one real put. 10 h.
12. **Code, then host.** `COMMANDS.md` from the parsers and the three hook JSON files from `hooks.client_settings`,
    with a test that the committed files equal the generated ones; the managed settings then need one
    `seal/setup.sh --update`. 8 h.

Steps 1 to 5 and 9 go in any order. 6 before 7, 7 before 8. Steps 10 and 11 last, each after a deploy. About
100 h in all, 22 of them on the host.

## What I could not judge

- Whether the console page still calls `/kb`, `/price` or `/tenants` of the portal. Only `/srv/awb-web/index.html`
  tells.
- Whether the key service should be the owner's only door to the bucket (step 11). It is the simpler design; it
  also makes a locked key service stop `awb import`, which today needs the vault alone.
- The real cost of the vault tests and whether their timing asserts take a clock stub without losing what they
  prove. They did not run here.
- Whether `deploy.import_map` keeps every restart decision after step 6. The proof is a dry run on the host.
