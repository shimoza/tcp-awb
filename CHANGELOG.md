# Changelog

What changed in the Architect Workbench, one entry per week (ISO week, Monday to Sunday), newest first. The form
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Customers and projects appear as codes only.

## Week 44, 2026-10-26 to 2026-11-01 (in progress)
### Added

- The move plan of the project files (task R5 of presentations/reports-design, branch r5-files-plan, sections 3.2 to 3.5, his yes of 2026-10-29 to the layout and the defaults). New `awb files plan [--laptop FILE|NAME|latest]... [--host FILE] [--buckets FILE...] [--put]` (awb/filesplan.py) reads the newest laptop inventory of `reports/inventory/` through the key service and the newest host and bucket inventories of `~/tcp-shared/files/`, checks the laptop rows again at entry, matches identical files by hash, probable ones by size and time and versions by group and kind, and writes `~/tcp-shared/files/plan-<stamp>/PLAN.md` and `PLAN.tsv` (id, place, action, target, project, path, keeper, reason): laptop `keep`, `move` into `YYYY-MM/tcp-xxxx/private|clean|out/` and `archive` into `YYYY-MM/tcp-xxxx/_archive/<stamp>/`; host and buckets `stale-in`, `duplicate` and `withheld`, listed only; nothing deleted; ids and coded paths only. `--put` sends the laptop rows to `reports/plan/<stamp>/PLAN.tsv` of the owner bucket for the laptop step R6; the key service's owner put takes that key and nothing else under `reports/plan/` (`bucket_layout.plan_key`). Tests: `tests/test_r5_files_plan.py`, no row replaced.

- The file inventories of the host and the buckets (task R4 of presentations/reports-design, branch r4-files-inventory, his yes of 2026-10-29 to the layout and the defaults). New `awb files inventory --host|--buckets [--out FILE]` (awb/filesinv.py): `--host` walks the project folders of the register as the work user (`sudo -n -u awb -H`), hash only, no content read; `--buckets` lists the owner bucket `awb` (project folders of live projects and `inbox/`, `reports/` and the dataset skipped) and the lab bucket through the key service, the etag as the md5 of a single-part object. The rows are those of the shared module awb/inventory.py of task R3 (one file per place: `host-<stamp>.tsv`, `owner-bucket-<stamp>.tsv`, `lab-bucket-<stamp>.tsv`; `--out FILE` takes all rows of the run). Every path passes the check of a text that leaves the host, a hit is `[withheld]`; a check that cannot run writes nothing. The key service has the new owner op `lab_list` on the admin socket (the top with "/", `inbox/` and `<code>/` of the lab bucket, list only) its owner `bucket_list` takes `inbox/` and the owner's CLI may read the laptop inventories `reports/inventory/<place>-<stamp>[-<label>].tsv` (`bucket_get`) for the plan of R5. Default place `~/tcp-shared/files/`. Tests: `tests/test_r4_files_inventory.py`, no row replaced.

- The laptop lists the files of the folders he names for the move plan (task R3, branch r3-clean-inventory, presentations/reports-design/DESIGN.md §3.1, his yes to the layout and the defaults of §7). New `awb clean inventory ROOT... [--project tcp-xxxx] [--label LABEL] [--put] [--include-onedrive]` (awb/cleaninventory.py) walks only the named roots, opens a file only to hash it and keeps the full list with the real paths in `~/awb-laptop/vault/inventory/<stamp>.tsv` (600, laptop only); the coded copy `~/awb-laptop/inventory/laptop-<stamp>.tsv` carries every path segment through the name machinery of `awb clean` and `[withheld]` for a path that still carries something. Skipped: the report folder, the report workbook, the laptop folder, `_archive`, `.git`, `~$` and `.tmp` files, `Thumbs.db`, `desktop.ini` and links; a root under OneDrive needs `--include-onedrive`. `--put` sends the coded copy alone to `reports/inventory/` of the bucket with the laptop key, the path of `awb clean put`. The row format of the three inventories (the eleven columns, their order, the file name and the header) is the shared module awb/inventory.py, for `awb files inventory` too; `bucket_layout.INVENTORY_PREFIX` names the bucket place. Tests: `tests/test_r3_clean_inventory.py`, no row replaced.

- The codes file of the report workbook reaches the laptop and the report workbook never leaves it (task R2, branch r2-clean-report, presentations/reports-design/DESIGN.md, his choice of 2026-10-29: a separate codes file the workbook links to). New `awb clean report [--dir DIR] [--save]` (awb/cleanreport.py) downloads `reports/projects.csv` and `reports/projects.meta.json` of the owner bucket with the laptop's own key through the proxy, the path of `awb clean fetch` (no new credential), checks the sha256 of the sidecar, keeps the file before as `projects.previous.csv` and replaces `projects.csv` in one step with three tries two seconds apart while Excel holds it; the folder is saved in `~/awb-laptop/report-dir`; it never uploads. The fingerprint of the report workbook is a workbook with both sheets Names and Projects in `xl/workbook.xml`, whatever its suffix (awb/reportbook.py). `awb clean FILE`, `--put`, `awb clean put` and the page of `awb clean serve` refuse a file in the report folder (the saved one or any folder named `awb-report`) and a workbook with the fingerprint with the line `the report workbook never leaves the laptop`; `awb clean fetch --to` refuses the report folder. `awb import` and `awb import --auto` hold such a workbook: nothing of it is imported, it stays in `in/` and the vault inbox, one line `file N of M held: ...` without its name and one mail per file to delete it from `in/`. The rule with its reason and its walls is `rules/report-workbook.txt`. Tests: `tests/test_r2_clean_report.py`, no row replaced.

- The codes file of the report workbook (task R1 of presentations/reports-design, branch r1-report-codes, his choice of 2026-10-29: the workbook lives on the laptop and links to a separate codes file). New `awb report --codes [--out FILE] [--put] [--if-changed]` (awb/reportcodes.py, command only in awb/deploy.py) writes `projects.csv`: one row per project of the register, the six columns `project code`, `customer code`, `task description`, `next step`, `whose move`, `date of last change` (a test pins the names), UTF-8 with a byte order mark, CRLF, comma, every field quoted, ISO dates, sorted by project code, `none` for no customer, whose move `owner`, `session`, `customer` (parked) or `closed` (30 days after the last change); both texts pass the name check, the gate detectors and the candidate rule again and a hit is written as `(withheld by the name check)`; a check that cannot run writes nothing. Beside it `projects.meta.json` with the keys `written` (UTC, ISO 8601), `rows`, `withheld`, `sha256` (of the exact bytes of projects.csv) and `release`; a test pins `sha256` and `written`, which the laptop side reads. Default place `/var/lib/awb-report-codes/`. `--put` sends both to `reports/` of the owner bucket through the key service, which allows those two keys there and nothing else (awb/bucket_layout.py REPORT_KEYS); a failed put is an item `reports: ...` of `awb owner status` until a put succeeds. The owner job `awb-report-codes` (seal/work-jobs, hourly at minute 50, `--put --if-changed`) is installed by the seal and by `setup.sh --update` (new list OWNER_JOBS) and proved by `verify.sh`. `status_of`, `_git_dates` and `withhold` of awb/period.py moved into awb/projectfacts.py; `awb report --period` is unchanged. Tests: `tests/test_r1_report_codes.py`, no row replaced.

- The dataset TCP Facts is published once a week by itself (task KS, branch ks-fact-schedule, his decision of 2026-10-28). New `awb dataset weekly` runs `awb dataset build` of today, `awb kb review` of that folder (his own mark; the refusal for the work user stays) and `awb dataset put --github`, each step only after the one before succeeded; the put keeps its checks (the review mark, the two README statements, no price data, the person scan). `seal/setup.sh` writes the owner unit `awb-dataset-weekly.service` and its timer (Monday 06:00 UTC, 07:00 Berlin in winter, a missed run made up at boot) in the new function dataset_weekly_unit and enables the timer; `seal/verify.sh` proves it enabled with the unit running `awb dataset weekly` as the owner. The result goes to `/var/lib/awb-dataset-weekly/last.json`: a failure is the item `dataset: weekly publish failed: <step>: <reason>` of `awb owner status` until a publish succeeds, a success the line `dataset tcp-facts-<date>: N added, M amended, K retired` of the 08:00 mail of the next 24 hours (sent for that line even when nothing waits on him). Tests: `tests/test_ks_fact_schedule.py`.

- A fact leaves the dataset TCP Facts only when something invalidates it, never for its date (task KS, his decision of 2026-10-28; tcp-facts-2026-10-28 had dropped 104 availability facts checked 2026-09-24 and 25 only because their 30 days had passed). `awb dataset build` keeps an expired fact with `"recheck_due": true` in facts.jsonl, `recheck due` after its check date in facts.md and topics/, a row of the new README table "Facts with a recheck due" and the count `recheck_due` in MANIFEST.json. It leaves out a retired fact (a superseded one is retired for its successor), one a recheck refuted on or after its check date (the new `~/tcp-shared/recheck/refuted.tsv`, one line per refuted verdict, counted as "contradicted by a recheck") and, as before, one that names a service the service description no longer offers. `awb kb export` is unchanged. Tests: `tests/test_ks_fact_schedule.py`, no row replaced.

- Knowledge base first (task KF, branch kf-kb-first; his decision of 2026-10-28: a session spent a 35-minute live run on an OBS 403 while facts of other projects already said that IAM-granted OBS rights take 20 to 30 minutes and a bucket policy works in seconds). The prompt hook adds "Known from the knowledge base (N facts on this topic; ...)" with one line per fact (id, grade, check date, statement) when a prompt starts a new task: the first prompt of a session, a prompt after more than two hours of silence or one that names a run, a customer question or a deliverable whose closest facts were not told in the session yet (awb/kbfirst.py). The facts are those of `kb.find` for the prompt, then for the goal and tags of SCOPE.md, each sharing at least two content words with the query, screened like `awb kb find`, at most ten and 1500 characters. A base that cannot be read adds nothing and never blocks; the client's agent and CI events get no block. The project CLAUDE.md start line gains "Before a new task or a run: read the knowledge base facts the hook shows ..." (`awb projects sync` adds it once) and the work rules a "Knowledge base first" line. `awb review claims` writes the column `known_before` (yes/no) into kb.tsv and prints how many claims were known before the work; the final check asks for the same yes/no column for each caveat and claim. Tests: `tests/test_kf_kb_first.py`; tests_replaced: the header assertion of `test_the_claims_get_their_knowledge_entries_and_one_can_settle_a_claim` (kb.tsv gained a column; successor `test_the_review_claims_say_which_claim_was_known_before`).

- Lab runs with the admin logins of the two lab tenants (task LR, branch lr-lab-runs, his decision of 2026-10-28: a project session tests its own scripts on test-10491 and test-10497 on his word, without asking him for a login). keys.conf takes `lab_admin = test-10491 test-10497` (no other alias); `awb keys unlock` loads `awb/admin/<alias>/domain`, `user` and `password` from pass. New ops `lab_run` and `lab_call` of the call socket (awb/tcp/labadmin.py): any method on any service, IAM, STS and OBS included, only inside an open run of the calling project; the service takes and caches the tokens, answers in full and shortens tokens, temporary keys and security tokens (awb/labsecrets.py); passwords and logins never appear. `awb lab run start|end|status` and `from awb import labclient` (awb/labclient.py, command only, core code that speaks the socket itself) for the scripts; the run records in `<vault>/lab-runs/` (awb/labruns.py); every create recorded with its delete, the end deletes in reverse; the call log in `notes/lab-runs/<run>.log`; old or failed runs in `awb owner status`. The work rules and the project template carry the lab run rule (`awb projects sync` adds it). Tests: `tests/test_lr_lab_runs.py`, no row replaced.

- A read-only tenant reads every project of its domain the login can see (task T2, branch t2-readonly-projects, reported by a project session: the sub-projects of eu-de and eu-nl were not readable). `awb tenant add-readonly` writes eu-de and eu-nl when `--region` names none (the keys.conf line `ALIAS:login:DOMAIN-ID:eu-de+eu-nl`; an old line of one region still works). `awb tenant regions ALIAS --region R [--region R]...` changes the regions of a registered one without asking for its login and runs `awb keys unlock`. `awb cloud call ... --tenant ALIAS --scope NAME|ID` names a region project or a sub-project (eu-de_<suffix>): the key service checks it against GET /v3/auth/projects (kept 5 minutes), refuses a project of another domain, an unknown one and one of a region the tenant does not read, takes a token scoped to that project by its id (one per project, in memory, renewed as before) and calls the endpoints of its region; a read key sends it as X-Project-Id. `awb tenant projects ALIAS` (the new op `projects` of the call socket) lists name, region and id of what a session can read. GET and HEAD only and the grant check of a call are unchanged; a scope on OBS is refused. Tests: `tests/test_t2_readonly_projects.py`, no row replaced.

- The laptop flow of `awb clean` with the project named once and a local page (tasks LC4 and LC5, branch lc45-clean-flow; the owner typed the project code four times and the file path twice for one file of a new project). `awb clean use tcp-xxxx` keeps the current project of the laptop and makes `~/work/tcp-xxxx` (`AWB_WORK_HOME`); `awb clean`, `awb clean put` and `awb clean fetch` take `--project` first, then the work folder of the current directory, then that project, and print `project: tcp-xxxx (from ...), customer CUST-XXXX` first. `--project` and `--kind` are no longer required: the kind is logs for a `.log` file and docs for any other (one run and one report per kind, the kind printed), the copies go into `clean/` of the work folder without `--project` and into `./clean/` with it, as before. A customer not registered on the laptop is asked for once on the terminal (no terminal: the old refusal). `awb clean put` sends the clean copies listed in the reports of the last run (`~/awb-laptop/last-run/tcp-xxxx`) without cleaning again; `--put` on the run is unchanged. A Windows path (`D:\Documents\x.docx`, `D:/Documents/x.docx`) is read as `/mnt/d/...`. `awb clean serve [--port 8765]` (new module awb/cleanserve.py, command-only in awb/deploy.py) is the same flow as one page of the standard library's http.server on 127.0.0.1: a random token per start in the printed URL that every request must carry, a Host header other than localhost or 127.0.0.1 refused, upload or typed paths, run, the report and a text view of each copy, put with the key of the serve process or the line how to load it; uploads in a temporary folder of mode 700 deleted after the run, no content logged. The guide has a section "Laptop flow" with the before and after. Tests: `tests/test_lc45_clean_flow.py`, no row replaced.

- The final check (task FX, branch fx-final-check): a last pass on a strong model for a high-stakes deliverable, from the one-off run of 2026-10-27 that found the lost caveats, a request dropped by contract.md, claims stronger than the evidence and answers out of the customer's order. `workflows/final-check.js` runs one agent pinned to Fable 5.1 (`model: 'fable'`) at effort max over input/, SCOPE.md, STATE.md, OPEN.md, evidence/, the review folder and the draft; it writes `deliverables/STEM.final.md` (the answer written again from the request) and `reviews/FILE/final-check.md` (the table); the script records the run in `reviews/FILE/final-check.json`. The tool guard passes it by its scriptPath like review.js and request-check.js (PD3), nothing else widened. The project template (awb/projects.py) has a section "Final check (FX)": his phrase "final check" or "финальная вычитка" starts it after one line on the cost and one word of his, never on its own; `awb projects sync` appends it to an older project. New `awb review show deliverables/FILE` lists the record state and the final check. `awb bucket put` does not ask for it. Tests: `tests/test_fx_final_check.py`, no row replaced.

- The final check stands in for the request check and the blind verification of its final text (task RV2, branch rv-review-fx, his decision of 2026-10-27). final-check.js asks its agent for the `sha256sum` of the final text and records it as `final_sha256` in final-check.json. While `deliverables/STEM.final.md` has that hash, its review runs `workflows/review.js` with `after_fx: true` (the platform lenses and the partner lens, no blind verification and no verify.json), `awb review pass` accepts it and records `final_check`. The send gate of `awb bucket put` takes the final check for the request check. After an edit or with a record without the hash the full review applies again and the pass and the gate say so. A final text is name-checked under the name of its draft, because the name check reads x.final.md as a web address: before, `awb review init` refused every final text ("url 1"). The template line of FX names the after_fx review; `awb projects sync` adds it to an older project. Tests: `tests/test_rv_review_fx.py`.

- `awb paste --own [--as NAME]` for our own output (task P1, branch p1-paste-own, the owner's rule of 2026-10-27: customer material is checked once, on the way in). A PoC script, a lab run on our own tenant or a key service answer gets only the secret finding of the gate (classes secret, private-key, token): each value becomes `[secret]` in place and the rest of the line stays; no name check, no structured pattern (phone, IBAN, MAC, mail, address, id), no refusal. It prints `own output: N secrets replaced` and the file, in `notes/pastes/` as before. A refused paste of the default mode is no longer lost: the raw text is kept 0600 in the pasting user's own `$XDG_STATE_HOME/awb/refused-pastes/` (default `~/.local/state`, the folder 0700, never in a project, never printed), the refusal names `awb paste --retry STAMP` and `awb paste --own --retry STAMP`, a written paste deletes the kept file and a kept file older than 7 days goes at the next paste. A `--as` name taken today is said in one line and written as `<NAME>-2` instead of a refusal. The project CLAUDE.md template and `awb projects sync` carry one new line: a script's output goes to a file first and then into `awb paste --own`, never a script piped straight into `awb paste`. Tests: `tests/test_p1_paste_own.py`; row T8-5 moves to its successor (a taken name now gets a number).


- Deliverables as HTML and md and notes from development (task DN, branch dn-notes-html; his rules of 2026-10-27: a link to a file on the host or in the bucket is useless to him, a text for a customer must paste into a mail, he relays nothing between sessions). `awb bucket put CODE FILE.md` writes `FILE.md` and `FILE.html` into out/: a small renderer in awb/bucket.py (`md_to_html`) renders headings, paragraphs, bold, italic, code, links as text, bullet and numbered lists, tables with borders and code blocks into one page with one inline style block, no asset and no script; the send gate is unchanged and judges the md. The output names both objects and ends with `get it: Total Commander, bucket awb, folder <YYYY-MM>/tcp-xxxx/out/, or on the laptop: awb clean fetch tcp-xxxx` (it replaces `on the laptop run: ...` after `--reveal`). `awb xchg put` of a Markdown file does the same in the lab bucket: after its checks the key service writes `put-<hex>.html` of the judged bytes beside `put-<hex>.md` in `<code>/from-session/<date>/` (one mail, by the id of the md), and the session prints both keys and `get it: Total Commander, bucket <lab bucket>, folder <code>/from-session/<date>/ (awb clean fetch on the laptop reads only out/ of the owner bucket)`. `awb clean fetch` brings both and prints the Windows path of the HTML to open; a Windows `--to` is read as /mnt/<drive>/... as before. New `awb projects note CODE --title TEXT FILE|-` (awb/devnote.py) writes `notes/from-dev/<date>-<n>.md` (mode 640) and a notice that the prompt hook shows once per session: `note from development: TITLE (notes/from-dev/FILE): read it first`; title and text pass the secret check only. The project CLAUDE.md gains two lines (no file links, deliverables as HTML and md, chat text as plain paragraphs; a note from development is followed like his word on the Workbench and never overrides his word on the project); `awb projects sync` adds them once. Tests: `tests/test_dn_notes_html.py`; tests_replaced: two assertions of `test_the_host_names_the_laptop_step_after_a_revealed_put` (the old last line and no fetch line after a plain put; successor `test_the_put_output_names_both_objects_and_the_get_it_line`) and one assertion of `test_a_put_lands_under_its_id_and_tells_the_owner_by_the_id_alone` (exactly one object after a put of a .md; successor `test_xchg_put_of_a_markdown_file_writes_the_html_beside_it_and_names_both`).


### Changed

- `sudo awb deploy` installs a changed unit of the work jobs (task SU, branch su-setup-update; on 2026-10-28 the new Saturday timer of `awb-kb-recheck.timer` had to be installed by hand because only a full seal ran step_work_jobs). `seal/setup.sh --update` runs the new step_work_units after step_units: each unit of `seal/work-jobs` is rendered as in the seal and installed with `install -m 0644` only where it differs, then one `systemctl daemon-reload` and `systemctl enable --now` and `systemctl try-restart` of each changed timer. A service is never started, an unchanged unit causes no write, reload or restart, cron.deny, the crontab, the other units and the package holds are not touched; `--units-only` leaves it out. Tests: `tests/test_su_setup_update.py`; tests_replaced: the update_steps assertion of `test_setup_update_mode_skips_the_seal_steps_holds_the_packages_hooks_the_kb_loads_the_egress_filter_adds_the_build_side_and_its_failure_oneshot` (the list without step_work_units) and the update plan assertion of `test_setup_dry_run_turns_the_crontab_of_the_work_user_into_root_timers_and_denies_it_cron` (no work-update or kb-commit line in an update); successor of both `test_su_the_update_path_calls_the_work_unit_step_after_the_units`.

- The weekly read tier of the fact-check (`awb-kb-recheck.timer`) runs Saturday 19:00 UTC (20:00 Berlin in winter) up to 15 minutes later, no longer Sunday 02:30 UTC (task KS, his decision of 2026-10-28: his token allowance renews Sunday 02:00, so the week's unused tokens are spent on Saturday evening). `seal/verify.sh` checks the time. Tests: `tests/test_ks_fact_schedule.py`; tests_replaced: the schedule assertions of `test_the_weekly_recheck_job_runs_the_read_tier_alone_as_the_work_user_and_is_installed_and_verified` (Sunday 02:30 UTC, at most 10 minutes; successor `test_ks_the_recheck_timer_fires_saturday_19_utc_at_most_15_minutes_late`).

- The weekly recheck takes every fact that expires before its next run, no longer only the expired ones (task KS: the run of 2026-10-25 02:32 UTC missed facts that expired later that day). `awb kb recheck-run` picks the entries of the new `kb.due`: expired or expiring within 8 days (the 7 days to the next run plus 1 day of margin). Tests: `tests/test_ks_fact_schedule.py`, no row replaced.

- A put never waits for a review (task GT, branch gt-no-gate, his decision of 2026-10-27 after a day lost to checks: customer material is checked once at the entry, after it nothing stops his work). `awb bucket put`, `awb xchg put` and the key service that receives a session's put no longer refuse a file for its review state (no record, a stale record, a tier, a lowered tier, a missing request check or final check), for a registered name or for structured data. One check stays: a secret (the gate classes secret, private-key and token) is replaced with [secret] in the uploaded copy, never in the file. The put prints one line, `review: none` or `review: passed DATE`, with the number of secrets replaced. The picture rules of `awb xchg put` stay. The stop hook no longer asks for a review and the name of a deliverable no longer blocks a write. The work rules and the project template say that a review runs only on his word and never ask him about tiers; `awb projects sync` takes the old put condition out of a project and adds the new line once.

- Logs of our own lab runs stay whole in a project (task GT, same branch, his decision of 2026-10-27). The write hook and the commit gate of a project no longer refuse ids or structured data (domain, project, user and resource ids, request ids, 32 hex ids, addresses, hosts, mails); a registered name still refuses. The write hook shortens secrets in place instead of refusing the file (awb/labsecrets.py): passwords and logins masked, temporary tokens, keys and signatures shortened to 8 characters and the length, a permanent key replaced with [secret]. The commit gate still refuses a secret. `awb paste --own` of P1 shortens the same way and now refuses a registered name (kept for a retry). The work rules and the project template carry the line on lab logs; `awb projects sync` adds it once.

- Two review levels with fixed names (task GT, same branch): level 1 "ревью" or "review" (the lenses of review.js and the request check, on the session's own model, findings only, no confirmation) and level 2 "глубокое ревью" or "deep review", also "финальная вычитка" or "final check" (final-check.js on Fable 5.1 at effort max after one line on the cost, about 200-300k tokens and 10-15 minutes and one word of his). Neither runs on its own. The section replaces the FX section of the project template; `awb projects sync` turns the old section into it once.

### Fixed

- Two kinds of false findings of `awb check` on a PDF of our own call log, read through pdftotext since T7 (task T8, branch t8-pdf-findings). A web address of a TCP endpoint cut by the line wrap (`https://obs.eu-de.otc.t-systems.c` at a line end and `om/...` on the next line) was reported by its cut head: a host whose head and rest are an allowed host only together (a public TCP endpoint or another host the patterns never report) is now read joined, and its cut pieces are no finding; a foreign host is not joined, so its cut head is still found. Digits of separate table cells (`018 | 018 | 3`, the same digits apart by a tab or three blanks of the layout) read as a phone number: a phone number no longer spans a tab or three blanks, and over a cell border it is one only from two cells with a subscriber number of at least four digits in the right one (`| 030 | 5551234 |` stays a phone). A phone number in running text is found as before. Tests: `tests/test_t8_pdf_findings.py`, no row replaced.

- Seven tooling defects a project session reported on 2026-10-28 (task T7, branch t7-tooling). A: the secret scrub of a put (`awb bucket put`, `awb xchg put`, the key service) and the shortening of the write hook read a PDF whose streams are ASCII as text and could replace stream lines; a PDF, a picture, an archive and an office file without a secret now go byte for byte (`putcopy.is_binary`, the gate's opaque rule). B: a call, an index, a format field and after `=` an attribute or a lower case variable are no password value (a getpass call with its prompt, an `os.environ` index, an `args.` attribute, a format field); a real value after a password word (a digit or a symbol in it) is still masked. C: an id that decodes to binary (16, 32, 40 or 64 hex digits, a uuid, a field name such as x-obs-request-id) no longer counts towards the 1000 encoded blocks after which `awb check` called a lab log opaque; base64 that hides text still counts. D: the tool guard reads the PATH of `awb cloud call METHOD SERVICE PATH` and a `--param K=V` of `awb lab run` as text, so `awb cloud call GET obs /` passes and `cat /` stays refused. E: `awb review` takes a number in a code block, a raw JSON line or an inline JSON object for data: "total" there makes no computed claim (R-005). F: the work user runs pdftotext directly (the unit needs the owner's sudo, so every PDF was opaque) and the gate reads a PDF through pdftotext instead of calling it opaque; opaque stays for a PDF that cannot be read or has no text layer. G: nothing found inside a hex id or a uuid (a phone number, an address) is a finding. Tests: `tests/test_t7_tooling.py`, no row replaced.

- A blind verification that checked nothing no longer counts (task RV1, branch rv-review-fx, found 2026-10-27: a project session filled the verdicts of claims.tsv before the tier 3 review, so the blind verification had 0 claims and the run looked complete). `awb review pass` at tier 3 refuses a verify lens of 0 claims while the text has high-risk claims whose row does not cite a blind answer of an earlier round (evidence/review/SLUG/C-N.md, which keeps V-14): "clear the verdicts set before the review in claims.tsv and run workflows/review.js again". The prepare step of review.js counts those rows (`preset`) and the script stops before it spends an agent on the blind phase. Tests: `tests/test_rv_review_fx.py`.

- The FX template line names `/opt/tcp-awb/src/workflows/final-check.js` (task T6): `/opt/tcp-awb/workflows` is a path no release creates. The tool guard compares the real path with the workflows folder of its own release (`src` links to releases/COMMIT). A test lays out a fake /opt and checks that the guard accepts exactly the paths the template names. `awb projects sync` moves the old line to the new path. The hand-made link of 2026-10-27 can go after the deploy and the sync. Tests: `tests/test_rv_review_fx.py`; tests_replaced: the old-path assertion of `test_the_template_carries_the_phrase_and_the_script_path` (successor `test_t6_the_template_names_the_installed_release_and_the_guard_accepts_exactly_that`).

- The owner's typed prompt is no entry of customer material (task T5, his rule of 2026-10-27: check once at the entry). The prompt hook stops his own words only for a registered name or a secret; a url, a mail, an IP address, a phone, an IBAN or a hex id he types passes (a paste block, an agent result and a CI event keep their rules). A file name with dots and a file suffix (answer-en.final.md, notes.v2.py) is no url anywhere; only www. makes such a name a host. Tests: `tests/test_t5_entry_only.py`; the prompt rows of T8 that pinned the old rule point to it.

- An IBAN no longer ends inside a word (task T4, found 2026-10-27: `awb paste` refused a PoC log for "iban 1"). The head of a 32 hex id (a project, domain or request id such as ba40b7...) passed the mod 97 check by chance about once in 97 ids whose first letters name a country; `_iban_ok` of awb/patterns.py now refuses a number whose last character is followed by a letter or a digit. Real IBANs, spaced or not, are found as before. Tests: `tests/test_t4_iban_ids.py`, no row replaced.

- The rules notice names a file the session may read (task T1, branch t1-rules-read, found in a project session): after a rules change the prompt hook told the session to read ~/.claude/CLAUDE.md again, and the pre-tool guard refused the Read, because ~/.claude is the client's folder. The notice of awb/rulesync.py now names the installed work rules file (`RULES_READ`, the same file as projects.RULES_FILE, which the project's CLAUDE.md imports and the seal copies into ~/.claude byte for byte). The guard is unchanged: the client's folder stays refused to every tool. Tests: `tests/test_t1_rules_read.py`, no row replaced.

- `awb paste` no longer masks platform error codes as `[phone]` and the short column names of a markdown table as `[mac]` (task T3, branch t3-paste-false-positives, reported by a project session on 2026-10-27). The cause of the phone: a phone may start right after a word and a dot (for `Tel.030 ...`), so the four digits of `Common.0018`, `APIGW.0301` or `ECS-0001` and the next number (a status code, the next cell of a table row) read as one number. Four digits right after a word of letters and a dot or a hyphen are now the code, unless the word is a phone label (Tel, Fax, Mob, Nr). The cause of the MAC: the view that reads two table cells as one value turned `| c0 | c1 | c2 | c3 | c4 | c5 |` into six hex pairs with blanks; a MAC found only across cell borders is dropped. Real phones and MAC addresses are still found, also a phone after an error code. The fix is in awb/patterns.py, so `awb clean --kind logs`, the host import, `awb import --paste`, the gate and the hooks gain it too. Tests: `tests/test_t3_paste_fp.py`, no row replaced.
- A password written in prose is a secret (task LC3, branch lc3-prose-password, found 2026-10-26: `awb clean --kind docs` wiped the key of a sentence and kept its password). The new rule `prose_password_spans` of awb/gate.py finds the value after a password word in English, German and Russian (is, was, lautet, ist, a colon, an equals sign or a dash with spaces, a plain space, an owner such as "for the admin" between): one token of 6 or more characters with a digit, a symbol or mixed case, or a quoted or backticked text of 4 or more. A date, a version, a placeholder (`[secret]`, `<...>`, `***`, changeme, redacted), a reference, code (brackets, a format field, a string built with `+`) and a word of the public lists are none. The commit gate, the paste door, `awb material add` and the hooks find it as a secret; the secret wipe of `awb clean` (awb/cleanlogs.py) puts `[secret]` in its place and keeps the words around it. The host import (`awb import`, `--auto`, `--trusted`) had no secret wipe at all and wrote keys and passwords into the outbox copy: it now runs the same wipe before the sanitising and removes a line the gate still finds a secret in, as `awb clean` does, so awb/cleanlogs.py is no longer a command-only module of awb/deploy.py (a change of it restarts what the intake restarts). A value already wiped (`password: [secret]`) is left as it is. The red-team case s-spaces-unquoted of calibration/redteam/gate.py builds its value from pieces like the other cases (the same string). Tests: `tests/test_lc3_prose.py`, no row replaced.

## Week 43, 2026-10-19 to 2026-10-25 (in progress)

### Added

- `awb owner status` is the one list of what waits on the owner, and `awb tenant add-readonly --login` asks for its values (UX review 2026-10-24, changes 4 and 5, branch ux45-owner-status-and-tenant). `awb owner status` collects the owner items of OPEN.md of every active project (a parked one only as a count), the rows of presentations/work-profile/QUEUE.md whose state waits on him (skipped when the file is missing) and the build tasks that are asked, red, held or green; each item is one line with the code or label, a few words, where it is done (project session, his terminal, steering session) and the ready command block; a text with a name hit is withheld. `--mail` sends it as one mail (nothing for an empty list or a name check that cannot run); the new owner timer `awb-owner-mail` of seal/setup.sh (new function `owner_status_unit`) runs it daily at 08:00 Europe/Berlin through the key service's notify, the path of the K15 owner mail. The old JSON status is `awb owner status --json`; `--write` is unchanged. `awb tenant add-readonly ALIAS --domain-id ID --login` in a terminal asks account name, user and password for the pass entries that are missing (the password hidden, never echoed or logged), writes them under awb/tenant/ALIAS/login/, registers the tenant, makes one test GET of IAM through the key service before any grant and says what failed (403 no programmatic access, 401 password wrong or change pending, MFA needed), and prints a short text for the customer's admin in German and English. Without a terminal or with the entries in pass it works as before. Tests: `tests/test_ux45.py`, no row replaced.

- `awb clean fetch tcp-xxxx [--to DIR] [--save]` (task ux2-laptop-fetch, branch ux2-laptop-fetch, UX review of 2026-10-24, change 2): a finished deliverable reaches the laptop. On the host `awb bucket put CODE FILE --reveal` already puts the file with the real names into `out/` of the project's bucket folder; it now ends with the line `on the laptop run: awb clean fetch CODE`. On the laptop the new command lists only `<YYYY-MM>/tcp-xxxx/out/` with his key from `AWB_OBS_AK` and `AWB_OBS_SK`, through `HTTPS_PROXY` like `--put`, downloads the files not fetched yet into `--to DIR` (or the folder saved once with `--to DIR --save`, for example a Windows folder under /mnt/c/Users/...) and prints one line per file. Key and etag of each fetched file go to `vault/fetched.tsv` of the laptop folder; a file put again with `--replace` has a new etag and comes again as `name (2)`. `in/` is never listed, read or written. Tests: `tests/test_clean_fetch.py` (fake OBS, fake proxy), no row replaced.

- `awb start ... --from DIR [--trusted]` and the import timer every 2 minutes (UX review 2026-10-24, changes 1 and 3, branch ux13-start-from-and-fast-import): `awb start project|query --goal ... --customer new --from DIR` runs the import inside the start on every regular file of DIR and its subfolders (a link or special file is skipped and counted, the "not a regular file" stop of a folder is gone), with `--trusted` when given, and its last line is `app: SSH host awb-10491, folder <the project's folder of the register>`; without `--from` the start is as before. The timer `awb-import-auto` runs every 2 minutes instead of 10 (only the OnUnitActiveSec line and the doc lines of seal/setup.sh). An import of the timer marks its notice, and the session of the project reads `new input: N files (FILE IDS)` (eight ids at most) at its next prompt, once; a manual import keeps its older notice text. Tests: `tests/test_ux13.py`; replaced: `test_setup_installs_the_import_timer_and_verify_proves_it_enabled` of `tests/test_import_auto.py` (it pinned `OnUnitActiveSec=10min`; row lc-laptop-clean-21 now runs its successor in `tests/test_ux13.py`).

- Public T Cloud Public service hosts pass the name check (task TH, branch th-tcp-hosts, found 2026-10-23): `nat.eu-de`, `csms.eu-de`, `com.t-systems.otc.eu-de.dns` and every host under otc.t-systems.com or t-cloud-public.com are no web address finding any more (API short form `<service>.<region>` for eu-de, eu-nl and eu-ch2, the VPC endpoint service name, the two platform domains at any depth); a host of any other domain, an e-mail and a name glued to a host still block. The blocks of `awb kb add`, `amend` and `verify` (and the kb commit hook) are recorded for the false-positive learning as door `kb`, strings on the owner's side only. Tests: `tests/test_tcp_hosts.py`, no row replaced.

- The grant refusal no longer prints a ready command with the session's customer (finding K14 of the review of 2026-10-20, branch k14-no-session-value): `awb projects kind CODE KIND` on a customer project without `--customer` shows the value of the row as information and tells the owner to type the customer code himself, from `awb register` or the start output, as `--customer CODE`. Tests: `tests/test_grant_customer_confirm.py`; replaced: the line test of row n20-grant-customer-confirm-1.

- A project can be parked (task PD4, branch pd4-parked-state, the owner's yes of 2026-10-22): `awb projects park CODE [--reason "waits on the customer"]` and `awb projects unpark CODE`. The register gets the state parked (the reason and the day are in `<shared>/parked.tsv`); folder, grants, bucket folder and tenants stay, `awb import`, `--auto`, the rules sync and the key service treat it like an active project, `awb close` still works on it, and the session start says it is parked. `awb board show` and `write` count it apart ("N active, M parked"), list it in a short Parked section after the active projects and leave its OPEN.md items out of every count and list (`GET /api/board` gets `parked`). For an active project the items under a heading of OPEN.md with closed, done, erledigt or abgeschlossen in it (any case) are not counted or listed any more, also by `awb close`. Tests: `tests/test_parked.py`.

- Read-only tenants (task RT, branch rt-readonly-login, the owner's yes of 2026-10-22): a customer or partner gives access to a tenant where nothing may change, sometimes only a console login. `awb tenant add-readonly ALIAS --domain-id ID [--login | --key] [--region R]...` checks by name that the pass entries are there (`awb/tenant/ALIAS/ak|sk`, or `login/domain|user|password`), writes the new `readonly` line of keys.conf and runs `awb keys unlock`; `awb tenant grant|revoke ALIAS CODE` lets one project read it; `awb tenant remove ALIAS` forgets it. The key service allows GET and HEAD only there, for every role and caller; any other method, a body, the lab role, a lease, a lab key, a secret and the console are refused with one line. A login gets a project-scoped token from IAM (POST /v3/auth/tokens, password), held in memory only, renewed 10 minutes before expiry and once after a 401, sent as X-Auth-Token and masked in every answer; MFA gives "this login needs MFA: ask for a read-only AK/SK" and IAM.0081 (console access only) its own line. `awb cloud tenants` shows the kind and key or login. Tests: `tests/test_readonly_tenants.py`, no row replaced.

- `awb review claims` keeps the rows a session added by hand (task PD1, branch pd1-review-manual-claims, found in tcp-w4pf on 2026-10-22): a re-run threw away every row whose sentence the generator does not list, so a hand-added row for a sentence of a reply draft lost its evidence and verdict and the findings it closed stayed open. Now a row that no generated claim takes stays, with its id, evidence, grade and verdict, while its sentence is still in the draft (its line is brought up to date); generated rows still follow the text. A row whose sentence is gone is dropped and named by its id in the output of `awb review claims`. `awb review l0` and `pass` judge such a row like a generated one (its verdict, grade and evidence; a high-risk row needs both). Tests: three new ones in `tests/test_review.py`, no row replaced.

- Two language chains in the work rules, the drafting skill and its plugin copy (task LG2, branch lg2-language-chains, the owner's word of 2026-10-22). Chain 1: when the customer writes German, the session talks to him in Russian, drafts and checks the English text, and after his accept `awb write translate FILE --to de --to ru` makes the German deliverable and his Russian reading copy in one step; the session shows him the Russian copy. Chain 2: when he asks in Russian or English the session talks English and the deliverables are English only, unless he asks for German or Russian; "по-русски" switches the talk to Russian until he switches back. The English note is gone: the session never comments on his English, dictation slips or punctuation (the work rules stay at 60 lines). Tests: four new ones in `tests/test_work_rules.py`; replaced: the pins "Reply in English" and "Never for Russian or German text" and the translate sentence in `tests/test_work_rules.py`, "English note" in the list of `tests/test_seal.py` (row P1 of `build/tests.json` renamed).

- `awb dataset put --github` refuses a build whose README lacks either public statement at its top: not an official product, and built only from public sources with respect for robots.txt (the owner's rule of 2026-10-22, task RS).

- `awb doc fetch URL [--out FILE]` and `awb doc hosts` (task DF, branch df-doc-reader, the owner's yes of 2026-10-22): a session could not read public documentation (the egress filter keeps the work user off every documentation host on purpose) and stated requirements from memory. The key service now reads one page for it with the new op `doc_get` of the call socket: GET only, https only, the host exactly a line of `rules/allow-domains.txt`, no port, user info, query string or fragment, a path of at most 300 URL-safe characters, at most 3 redirects and only to listed hosts, at most 5 MB, HTML, Markdown, plain text or JSON only, HTML turned into text with the standard library, no cookie, no auth header, a fixed User-Agent. At most 30 fetches a minute and 500 a day per project; one line per fetch in `<vault>/log/doc-YYYY-MM.tsv` (time, project, host, path, size, status, never content). The text comes with the line `source: URL, fetched YYYY-MM-DD HH:MM UTC`. The work rules and the drafting skill: such a page is read with `awb doc fetch URL`, quoted with its source and date, never stated from memory when it can be fetched (the rules stay at 60 lines). A host off the list is refused with one line telling the session to leave one owner item in OPEN.md under the owner heading, `Steering: allow documentation host HOST (vendor, why)`, never a question. `rules/allow-domains.txt` gains the official documentation hosts docs.redhat.com, access.redhat.com, documentation.ubuntu.com, help.ubuntu.com, www.cisco.com, techdocs.broadcom.com, docs.vmware.com, docs.nvidia.com, docs.otc.t-systems.com and docs-beta.otc.t-systems.com (every line before stays). Tests: `tests/test_doc.py`.

- `awb clean doctor` (task lc-doctor, the owner's yes of 2026-10-22): the laptop behind a corporate proxy. It prints the proxy variables (the host and port only, never a user or a password of a proxy URL), sends one HEAD request through the environment's proxy to the object storage of the region and to github.com and names the fix of a failure: the proxy cannot be reached, the proxy wants a login, or TLS verification failed (the root certificate of a TLS-inspecting proxy through update-ca-certificates, then SSL_CERT_FILE). The download of `awb clean update` builds a fresh opener, so it reads the proxy of the environment as it is now. Tests with a local fake CONNECT proxy show that `--put` and `awb clean update` (download and git) go through `HTTPS_PROXY`.

- `awb clean` and `awb import --auto` (task lc-laptop-clean, branch lc-laptop-clean, the owner's yes of 2026-10-22): the anonymization of project material moves to his laptop (Ubuntu under WSL), so the host gets clean files and imports them fast. The laptop edition needs no vault daemon, no key service and no work user: `awb init --laptop` (or `awb clean init`) makes `~/awb-laptop` with its vault folder, `awb clean register CUST-XXXX` asks the written forms once on the terminal. `awb clean FILE... --project tcp-xxxx --kind docs|logs [--out DIR] [--put]`: `docs` writes the body text of docx, pptx, xlsx, odt, pdf (and text, html, mail) files with pictures, properties, headers, footers and comments left out and the names wiped by the rules of wipe mode; `logs` wipes users, hosts, IP addresses, mail addresses, phones, keys, tokens and passwords of logs, configs and tickets and guesses no name (awb/cleanlogs.py). A report per run gives kinds and counts, the mapping stays in the laptop's vault folder, nothing opens a network connection (a test makes socket creation raise). `--put` puts the clean copies into `<YYYY-MM>/tcp-xxxx/in/` of the bucket with his key from `AWB_OBS_AK` and `AWB_OBS_SK`. `awb clean update` fetches the newest lists of public words from the public repository (a `git pull --ff-only` of the clone, or a download checked against the blob ids of the tree) and prints what changed. On the host `awb import --auto` (the owner's timer `awb-import-auto` of seal/setup.sh, every 10 minutes) takes the new files of `in/` of every active project, a clean copy with `--trusted` and any other file in wipe mode, moves the original into the vault, deletes it from `in/` through the new key service call `bucket_drop_in` (a file of `in/` of an active project only), leaves the "new input" notice, skips a project whose import runs (every import now holds a lock per project) and logs one line per run. `--trusted` no longer learns a form from a guess (it wiped the guessed words wherever they stood again) and no longer runs the candidate rules at all. The measurements are in presentations/strategy/LAPTOP-CLEAN-2026-10-22.md of the owner's folder.

- `awb kb import FILE` (task PG1, branch pg-kb-import-and-lite-plugin, the owner's word of 2026-10-21): partners get three levels of TCP Facts and the middle one had no way in. `awb kb import FILE [--update] [--release LABEL] [--dry-run]` reads the `facts.jsonl` of a TCP Facts release (or its zip) into the knowledge base: each line becomes an entry with its statement, scope, tags, grade, checked date, class, expiry and source, the source starting with `imported: tcp-facts <release date>`. Release ids are kept when free and never overwritten: a taken id is skipped and `--update` replaces only an entry of the same dataset with an older checked date. Every fact goes through the checks of `kb add` including the name check (the near-duplicate check is left out, a release holds facts that read alike on purpose); the counts say imported, updated, skipped and refused by class. It needs no vault daemon, no key service and no work user. The README gets a laptop section.

- The translation check keeps facts, not English spellings (task TR, branch tr-translation-keeps-facts, his word of 2026-10-21): in the across mode of `awb write translate` and `awb write keep` a number with its unit (day, week, month, year, hour, GB, TB, vCPU, Mbit/s; English, German and Russian forms) is one fact (12-month, 12 Monate, 12 месяцев), a rate is one fact (EUR/month, EUR/Monat, EUR/мес.), a date is one fact (2026-10-21, 21.10.2026, 21.10.26, day, month and year read in the fixed order of each form), a number has one value in every format (1,340.15, 1.340,15, 1 340,15) and an identifier inside a hyphenated word counts (g7v-Host holds g7v, CAD-Session-Hosts holds CAD, NVIDIA-Lizenz holds NVIDIA). A changed value, unit, date, amount or a lost identifier is still refused. Within one language nothing changes. Tests: `tests/test_translate_facts.py`; no row replaced.

- Empty bucket folders open in every S3 client (task BK, branch bk-keep-files, his yes of 2026-10-21): `ensure_folder` also writes a one-line `.keep` into `in/` and `out/` of each project folder when missing, `awb bucket sync` adds it to every existing project folder that lacks it (never twice), and the key service accepts a put of exactly `<month>/<code>/in/.keep` (out/ already took it). Pull, fetch, `awb import`, the inbox and the console listing of `in/` ignore `.keep`. One test of `tests/test_bucket_layout.py` that compared the exact set of keys of a new folder is replaced by `tests/test_bucket_keep.py` (row B-026-1).

- The name check learns from its false positives (task FP, branch fp-check-learns, his yes of 2026-10-21): every block of the prompt hook, the gate, `awb check` and `awb import` is recorded with its rule, class and time, the blocked string only inside the vault (`<vault>/log/fp/blocks-YYYY-MM.tsv`, 0600; the work user's check goes through the vault daemon, which writes the line itself). `awb check learn` (owner, nightly timer `awb-check-learn.timer`) learns a string from the public corpus or the log rules for support logs (hosts and FQDNs, ids and hashes, error codes, HTTP status lines, paths, timestamps, service names) and keeps first names, titles, legal forms, registered and person-shaped strings blocked; the learned list is `<vault>/log/fp/allow.tsv`, one exact string of one class (url, ip, mac, phone), read by the check and the vault daemon. A safety gate runs the planted names and the red team wipe pack (405 cases) before each change and drops a change that lets a planted name pass. One line a week by mail. `awb check allow WORD` and `awb check allow --list` (the owner's terminal only). The synthetic support-log set `calibration/fp/support_logs.py`: 15 of 15 harmless lines blocked before, 0 after; 6 of 6 planted lines blocked before and after.

- `awb mirror console` (task cs5-mirror-console, his "CS-D yes, mirror" of 2026-10-20): the owner's private mirror of the TCP console screens (`~/tcp-mirrors/console/<date>/`, one folder per walk, `INDEX.tsv` with seven columns and per service `NN-slug.jpg` or `.png` and `NN-slug.txt`) as a searchable source. `awb mirror console find WORDS [--service SVC] [--limit N]` ranks the screens of the newest walk by word overlap over the note, menu path, title and text (as `awb kb find` does) and prints service, file, menu path, URL path and the first matching line; `awb mirror console show SVC/NN-slug` prints the INDEX line, the text and the image path; `--walk DATE` names an older walk. Both are read only, so the work user runs them against `/srv/tcp-mirrors`. `awb mirror status` lists the console mirror: walks, newest date, screens and services. The mirror is never in the public dataset and never in git. The drafting skill and the work rules: a console how-to cites the screens with their date and writes a step the mirror lacks as "not in the mirror yet: check on screen", never from memory (one sentence in the work rules, which stay at their 60 lines).

- `awb howto` and `awb mirror console publish` (task CS7, branch cs7-howto-from-mirror, his ask of 2026-10-21): step-by-step console how-tos with screenshots from the console mirror. `awb mirror console publish [--to DIR]` (owner side) copies the newest walk to `/srv/tcp-mirrors/console/<date>/` (folders 0750, files 0640, only INDEX.tsv and jpg, png and txt files, no link followed, built beside the target and renamed into place) where the work user reads it; `find` and `show` already read that root for the work user and `~/tcp-mirrors` for the owner. `seal/setup.sh` makes the folder, owned by the owner with the group of the work user and the setgid bit, so the work user cannot write there. `awb howto draft "TOPIC" [--service SVC] [--step WORDS]...` writes `deliverables/howto-<topic>.md`: a title, a short "before you start" with the date of the walk, then numbered steps in the order of menu path and step number, each with the menu path, the labels as the screen spells them, the screen and its screenshot (copied to `deliverables/images/`, only the cited ones); a step the mirror lacks reads "not in the mirror yet: check on screen"; an existing draft is not overwritten without `--force`. `awb howto render FILE` writes one self-contained HTML file with the images as data URIs; `--pdf` waits (no standard-library route) and exits 1 after writing the HTML. `awb xchg put` treats a page with embedded pictures like a picture: it needs `--image` and a reason. The drafting skill and the work rules: a console how-to starts from `awb howto draft`, never from memory.

- Console screenshots stay internal (task ST, branch st-screenshots-internal, his decision D-ST of 2026-10-21): `awb howto draft` now writes the outward text by default, exact steps with the labels as the screen spells them, no image link and no source; the sources and the date of the pass go into `reviews/howto-NAME/sources.md`; `--internal` writes the image version. `awb howto render` refuses a draft with images unless `--internal`; an internal page says "internal, never publish" at the top. `awb xchg put` refuses an HTML or Markdown file that embeds or links an image, with or without `--image`, unless the owner passes `--internal-image` in his own terminal. The work rules, the drafting skill, the README, the command guide and the help texts call the source a private console reference. Tests: ten new ones in `tests/test_howto.py`; six rows of CS7 are replaced (`cs7-howto-from-mirror-1`, `-3`, `-4`, `-6`, `-8`, `-11`).

- `awb import CODE --trusted`: the owner vouches that the files hold no name, so no name is guessed; registered
  forms, secrets, mail addresses and phones are still wiped (his ask 2026-10-20).

- The key service binds a session's call and lease to its own project by a key token (K4 of the review of 2026-10-20, again, task k4-scope-across-users): `awb spawn` writes `.git/awb-key-token` into the project folder (mode 640, never tracked by git), `awb projects kind CODE project` records it in `<vault>/key-tokens.json` (mode 600), the awb client sends the token of its working folder with each call and lease and the key service takes the project from the token alone. A session of one project that names another project or sends no token is refused before anything is signed; the owner's CLI is not bound. It reads nothing of the caller's process, so it works across the owner and the work user. A project spawned before it gets its token on its first `awb cloud` call and needs `awb projects kind CODE project` once more.

- `awb material add [--kind mail|transcript|table|note] [--title WORDS]` (task CS6): the owner pastes text that holds no name straight into the project session. A message that starts with `material:` makes the session pipe the text after the word, unchanged, to the command, which runs the secret scan, the home-path rule, the owner blocklist and the name check through the check socket of the work user. A hit refuses the whole text with its class and line and writes nothing. On a pass the text lands in `input/` as `note-<date>-<n>.md` (or `mail-`, `transcript-`, `table-….csv` by kind), a ledger line is added and the file name is printed. `awb import CODE --paste` is unchanged. The work rules (`seal/work-claude/CLAUDE.md`) get the `material:` rule.

- The key service no longer checks the caller's project folder through the caller's process (K4 of the review of 2026-10-20, PR 69, reverted): the key service runs as the owner and cannot read the process of a session of the work user, so every session call was refused. K4 comes back in another form.

- `awb mirror rn` (task rn-release-notes): the release notes of T Cloud Public as a third public source next to the docs mirror and the service description. It reads the list page one request per page with a pause (`--pause`, default 2 s) and a User-Agent that names the Workbench, never a note page, `/neos/` or an image. It keeps one file per note under `<mirrors>/release-notes/notes/` plus `INDEX.json`. A known note is never written again; once every page has been read a run stops at the first page without a new note (`--pages N` caps a run). `awb mirror status` shows the count. `awb refresh` gets the part `release-notes` (after `mirrors`): for each note first seen since the last refresh it lists `recheck: KB-XXXX (release note DATE SERVICE)` for the entries whose tags or statement name the service and `new service?` for a service `rules/services.tsv` does not know. It changes no entry. The first run reads the notes dated in the last 30 days.

### Changed

- `awb dataset put --github` never pushes an old local history back (task PX, the owner's rewrite of the published history of 2026-10-21): it fetches the remote fresh (branches and tags) and brings the clone to the remote branch, a fast-forward when it is behind and a hard reset when the local history diverged. Before the push it fetches again and refuses a non-fast-forward push in one line. No push uses `--force` any more; a replaced tag is deleted on the remote and pushed again. `plugin/README.md` carries a short independence line (RA-M). The README of the dataset gets a centred header in the style of this repository (title, subtitle, the disclaimer "Unofficial and independent", a row of shields.io badges: Unofficial, Facts, Best before, License, MCP server), and `README.md` of this repository opens with the same disclaimer and an Unofficial badge first (FK). The dataset tests use invented tenant aliases (`TENANT_ONE`, `TENANT_TWO` of `tests/fixtures.py`, the form `test-` and five digits, which the scrub matches); the real aliases are in no file of the repository any more.

- The TCP Facts dataset ships no price list any more (task PX, the owner's order of 2026-10-21: a legal check found the full price list of a region may be a protected database, § 87a and § 87b UrhG). `awb dataset build` writes no `prices/<region>.csv`, `MANIFEST.json` lists none and the README, PROMPT.md, HOW-TO.pdf, facts.md header and the extension manifest no longer mention a price file or "the public price list of two regions"; they say prices come live from the public price API with `--live` and are not part of the dataset. The build keeps its live price sample check but writes nothing of it into the folder. `tcp_price_find` of the MCP server works only with `--live`; without it the answer is "prices are live only" with the way to turn `--live` on, and with `--live` there is no snapshot fallback. `tcp_calculate` and the facts that state one price with source and date are unchanged. `awb dataset put --github` removes a `prices/` folder the published repository still holds (git rm in the new commit, named in the commit message). Seven old tests that asserted the price file or the snapshot answer were replaced (see the pull request); new tests are in `tests/test_dataset_px.py`.

- `awb import` says what it does and is about three times faster (task ip-import-progress-speed, the report of 2026-10-20 and 2026-10-21: 13 small Markdown files took about 90 seconds and printed nothing until the end). It prints `checking 13 files` on standard error before the slow part and `file 3 of 13: F-XXXX, 0.8 s` after each file (the file id and the seconds, never the name of the original). The time went to work done again for a text it had seen before: the hundreds of stop phrases each scanned over every paragraph, and the structured-data search, the code and tech-term scans, the runs of capitalised words and the candidate rules run again for the same paragraph at each step. Each of them now remembers its answer for the text (the candidate rules also for the same known spans and the same learned forms), and the stop phrases run only when their first word is in the text. What is wiped did not change: the wipe red team (405 cases) gives the same leaks and losses before and after.

- Fewer false candidates in the intake (task nq-fewer-false-candidates): `awb import --review` no longer asks about plain English words (Outcome, Worked, Scorecard, APPEARED), hardware and hypervisor terms (KVM, Sapphire Rapids, Hyper-V), shell fragments or section labels (`D. Built`). New lists `rules/common-words.txt` (88,000 lowercase words of the public-domain Moby lists, without names and places) and `rules/tech-terms.txt` (CPU generations, hypervisors, Windows and security terms, Linux tools), read by `awb/words.py` (`load_plain`) and `awb/intake/wipe/lexicon.py`. A backtick span, a fenced block and a shell command line give no candidates. Two capitalised words in a row and every first name of `rules/first-names.txt` stay candidates. No existing list or test changed.

- One code per project (task one-code-per-project): `awb start`, `awb spawn` and `awb import` with `--customer new` issue `tcp-xxxx` and `CUST-XXXX` from the same four characters, and the bucket folder is `YYYY-MM/tcp-xxxx/`. A pair of which either code is taken is drawn again; `awb import` on an existing project refuses a taken `CUST-` code in place of a random one. `awb start` and `awb spawn` refuse `--customer CUST-XXXX` with one line (every project gets its own code); `--customer new` or no customer. `awb spawn --customer new` is new. `awb start --customer new` takes an empty first line at the forms prompt: a fully anonymous project with no written form. Existing projects keep their codes.

- File names and the review tier (task xg-names-and-review-tier): `awb xchg put` and the review commands (`init`, `l0`, `pass`) propose a neutral file name when the name check refuses the file name: `<project code>-<kind word>-<language>.<ext>`, for example `tcp-q7m4-mapping-de.md` (`awb xchg put --kind WORD --lang XX` choose the words, default `doc` and the language read from the text). The check of the name stays as it was. The session template and the work rules tell a session to name deliverables that way from the start. `awb start` and `awb spawn` take `--review-tier 1|2|3` (default 3, no line written), written as `- review-tier:` into SCOPE.md; `awb projects tier CODE N` changes it later as the owner. `awb review init` without `--tier` takes the tier of the project (`--tier` raises it for a file), and the send gate refuses below the tier of the project instead of below 3 (`--lowered` works as before).

- The gateway (task B-029): `make_server`, `make_front_server` and `_configure` of `awb/tcp/web/gateway.py` no longer take the unused place of the removed portal's port; the ask port is now the fourth argument. The command line of the gateway is unchanged.

- The web API contract (task B-029): the descriptions of `/api/kb`, `/api/price` and `/api/usage` in `awb/tcp/web/contract.py` and `docs/api/openapi.yaml` describe the answer shape on its own and no longer refer to the pages of the removed portal.

- `awb refresh --part release-notes` lists fewer entries (task rn3-narrow-release-notes): an entry is listed for a note only when it names the service and shares at least two topic words with the note (words of the title or first paragraph against the statement and tags; stop words, the service names and generic words such as service, cloud, new or support do not count). The lines run by shared words, most first, 15 per note and then `and N more (awb refresh --part release-notes --all)`; `--all` lists every match. A note dated after today goes to its own block `announced` with its date.

### Fixed

- The send gate trusts the fingerprint of a closed project only when it matches the hash pinned at close (task k16-k20-host-hardening, K16 and K21 of the review of 2026-10-20). `awb close` pins the sha256 of the fingerprint it wrote in `<vault>/fingerprint-pins.tsv` (mode 600, the owner's); as the work user it asks the vault daemon (new op `pin` of the check socket, the code only), which reads and hashes the shared file itself. The first pin of a code wins, another hash later makes the code a conflict. The gate refuses a closed project without a pin, with a conflict or whose file changed since, so an emptied fingerprint no longer lets a deliverable of that project pass. Closing a closed project again keeps the salt, so its bytes and its pin stay. Every build unit's own `/dev/shm` is now also `nosuid,nodev` (`seal/verify.sh` probes the same). K20 (the extract unit) is not in this change. Tests: `tests/test_k16_k21_host.py`; replaced: the `/dev/shm` strings in `tests/test_build_units_ipc.py` and `tests/test_build_runner.py` (rows n10-build-units-ipc-1 and SB3-31), the review K21 asks for nosuid,nodev.

- The lab write list of keys.conf matches whole calls (K9 of the review of 2026-10-20, branch k9-lab-writes-triples): `lab_writes` is now a comma-separated list of `METHOD service path` triples (`POST ecs /v1/{project_id}/cloudservers, PUT obs /{name}?policy`). A write of the lab key passes only when its method, service and path are one triple: a `{name}` segment stands for exactly one segment, never a prefix, and the query keys must be the ones after `?` (none without it). Without the line the key service signs no write of the lab key and says so in one line; `awb keys unlock` prints the count of triples or that no write is signed. The old `service:path-prefix` form no longer stops the unlock (a deploy must not take TCP from every session): the key service unlocks, reads work, and it logs and reports one line, "lab_writes has the old form: no lab write is signed until `awb keys lab-writes --from-recipes` writes the new line", and signs no lab write. `awb keys lab-writes --from-recipes` prints the 50 triples of the recipes and the recheck watch (each shape once), so `POST ecs /v1/{project_id}/cloudservers` no longer passes every ECS v1 write and no `obs:/` passes every OBS write. The owner pastes the new line into keys.conf and runs `awb keys unlock`. Tests: fourteen new ones in `tests/test_keys_lab_writes.py`; replaced: six rows of n17 and the tests that relied on writes without the line (named in the commit).

- The extract unit of the pdf reader gets the isolation of the build units (task k20-extract-unit, K20 of the review of 2026-10-20, the owner's yes of 2026-10-23): `PrivateIPC=yes`, its own empty `/dev/shm` (`TemporaryFileSystem=/dev/shm`, root 0755, so the unit's user cannot write there), `ProtectProc=invisible`, `RestrictAddressFamilies=AF_UNIX` and `PrivateDevices=yes`. The sudoers line of `seal/setup.sh` and both probes of `seal/verify.sh` carry the same fixed form. The /dev/shm has no options because `:` and `,` are sudoers syntax. The uid stays the fixed `awb-extract`: `DynamicUser=yes` with that static user changes nothing, and a dynamic uid would drop `--uid` from the fixed form. Tests: `tests/test_k20_extract_unit.py` (a real pdf through the unit's argv, no network socket under the unit's socket rule in the user's own service manager, visudo parses the line); replaced: `test_the_unit_argv_is_the_fixed_form_with_the_user_of_paths_conf` in `tests/test_extract_pdf_unit.py` (row n18-extract-sandbox-1) by `test_the_unit_argv_is_the_fixed_form_with_the_k20_isolation`.

- The tool guard no longer refuses honest calls inside the own project (PD2, tcp-eqew, 2026-10-22): a / of text (a sed or awk program, a quoted sentence, a division, a here-document or here-string body, a / glued to an option such as `-F/`) was taken for the root, which holds every project, and a path in a folder the session cannot read failed the project lookup as "cannot be determined". Text is now refused only when it names a place inside a refused one; `$(( ))` is arithmetic; the body of `bash <<EOF` is read as commands and the `$(...)` of an unquoted body too; the project is also found from the session's first folder (its transcript folder) when a cd left it. A real path into another project, a key folder or the client's folder, and `ls /` or a search of the home, are refused as before. Tests: `tests/test_pd23_guard.py`.
- Project sessions can run the review workflows (PD3, tcp-w4pf, tcp-s5i3): the work user's settings no longer deny `Workflow`; the pre-tool hook takes the Workflow tool and passes only `workflows/review.js` and `workflows/request-check.js` of the installed code by their scriptPath (no inline script, no workflow by name, since a script could start another workflow from any file where no hook sees it). Their agents run in the session's client under the same settings, deny rules and hooks. Two tests that pinned the old matcher and deny list are replaced (rows TG-13 and pd23-14).

- Setup creates the notices folder of the shared side (owner writes, work user reads): `awb import` ended in a
  PermissionError after writing the copies and sessions never saw "new input".

- `awb import` into a project whose customer was issued without a form no longer asks for a form (CG3).

- A build task's own /dev/shm is writable for the task user again (mode=1777): checks no longer fail the two ledger
  tests on a lock they could not create (SH).

- The owner mail for a command in OPEN.md is sent by the owner's timer (K15): `awb build nudge`, every 15 minutes, now also calls `projects.mail_owner_commands_all`, because the stop hook runs as the work user and the key service refuses that user. The mail carries a command only when it is on the allow list (`projects.OWNER_MAIL_COMMANDS`: keys and vault unlock, unlock, projects kind and delete, close, cloud sweep, import, build show, retry and widen) and the item passes the name check; a check that cannot run mails nothing.

- `awb voice learn` no longer follows links (K11): a draft that is a link, or that lies behind a link inside the project folder, is refused with and without `--project`; the voice folders are made with `config.make_dir(..., shared=True)`, which refuses a planted link instead of chmodding what it points to.

- OPEN.md and RESOURCES.md lines no longer carry terminal escapes to the owner (K10 of the review of 2026-10-20): the items of `awb`, `awb close` and `awb projects` and the live resource lines drop control and format characters (ESC, C1, bidi marks) before they are printed.

- `awb deploy` and a change of `rules/` alone (task fix-rules-reload): a change of any file under `rules/` now counts as a change for every daemon whose code imports `awb/patterns.py`, which reads `rules/allow-domains.txt` at import. The vault daemon reloads and stays unlocked, the others restart. Before, only the Ask page and the chat restarted, so a new allowed host was refused by the running name check until `awb vault reload` by hand (2026-10-20).

- The build runner restarts after a deploy again (task fix-runner-restart). With the web side live a deploy handed every service it restarted to `seal/web/install.sh --only`, which restarts only the units its own lines name and skipped `awb-buildd.service`; the deploy still marked it restarted. The runner kept the code of 2026-10-14 for six days and about thirty deploys, so the protected path check of R2 never ran. Now a service `install.sh` does not name restarts through `systemctl restart`. The runner also exits at its next idle moment, after a task step or a check, when the live release is not the one it runs, even without `restart-wanted`. It keeps `runs/runner.state` (pid, release, idle or busy). `awb deploy status` names that release and exits 1 when an idle runner runs an older release than the live one; `seal/verify.sh` runs it.

- `awb kb recheck-watch` no longer deletes a resource by a name match alone (task B-029, finding R8 of the review of 2026-10-16). A server, disk or address is taken only when its `awb-project` tag names the service project. A VPC, subnet, security group, WAF policy, SMN topic or subscription and RDS instance, whose listings carry no tags, is also taken by its lab name with the project code and an `awb-expiry` marker that has passed. A resource named like the project without the tag or the marker is left alone and listed by its handle.

- The front door of the gateway limits a request by one deadline (task k-gateway-limits, K17): the request line, the headers and the body together get `UNAUTHENTICATED_TIMEOUT` (5 s), the body of a request with a session `AUTHENTICATED_TIMEOUT` (20 s). Before, each read waited that long, so a client sending one byte at a time held its thread without limit.

- The status socket of the gateway drops an idle client after 3 seconds (task k-gateway-limits, K5): `StatusHandler` had no timeout, the socket is 0666 and its threads share the `TasksMax` of the gateway, so any local uid could hold the site with idle connections.

- `awb refresh --part release-notes` (task k-release-notes-hardening, K13): a title or a service name from the release note page reaches `REPORT*.md` and `release-notes.txt` as printable ASCII only (escape and control characters and any other character become `?`) and cut to 80 characters, so a hostile page cannot put prompt text or terminal escapes in the shared tree.

- `awb mirror rn` (task k-release-notes-hardening, K24): a redirect of the list page is followed only to https on the same host. A redirect to another host (a metadata address included) or to plain http ends the run with `the release notes answered HTTP 30x`.

- `awb mirror rn` (task k-release-notes-hardening, K23): the date of a note is read as ASCII digits and must be a real calendar day. A date in Arabic-Indic digits or one such as 2026-02-30 leaves the note out, so it can no longer sort after today and pass the first-run floor.

- `awb mirror rn` (task k-release-notes-hardening, K12): one malformed list page no longer ends `awb refresh` with no report. A pager number reads at most 6 ASCII digits, and a body that is cut (`IncompleteRead`) or a reset connection is reported as `release notes: not updated (the release notes could not be fetched ..., page N)` like any other failed fetch.

- `awb mirror rn` (task k-release-notes-hardening, K6 of the attacker review of 2026-10-20): a hostile list page can no longer make a run unbounded. The pager is read up to 200 pages, one page is read up to 2 MiB and 60 seconds in all, and a page holds at most 100 notes with a title cut to 200 characters, a text to 2000 and at most 10 tags of 80.

- `awb/extract/pdf.py`, `awb/config.py` (task k-extract-sandbox, K19): the pdf reader ran poppler direct whenever `host_conf()` came back empty, also when `/etc/awb/paths.conf` was there and could not be read. `host_conf(strict=True)` now raises on a file that is there and unreadable, and that pdf fails; a file that is not there or names no `extract_user` is still direct.

- `awb/extract/pdf.py` (task k-extract-sandbox, K18): the output of the extract unit is written to files with a size limit of 64 MB (`MAX_OUTPUT`) instead of a pipe read whole, so a crafted pdf can no longer fill the memory of the owner's intake. A unit that writes more fails that file.

- `seal/setup.sh`, `seal/verify.sh` (task k-extract-sandbox, K7): the sudoers line of the pdf extraction joined the two tools with a comma after one prefix, and a comma starts a new command in sudoers, so the second command was a bare `pdfinfo` as root and the unit's own `pdfinfo` call matched nothing. Every tool now carries the whole fixed `systemd-run` prefix. `seal/verify.sh` gets a second check, `the unit of awb-extract runs pdfinfo on a one page pdf`.

- `awb dataset put --github` (task k-dataset-signing, K1 of the review of 2026-10-20): every git and gh call on the clone sets `core.hooksPath=/dev/null`, `core.fsmonitor=false` and `gpg.ssh.program=ssh-keygen` (through `GIT_CONFIG_COUNT`, the scope of `git -c`), so a setting a session plants in the clone runs no program as the owner and never reaches the signing key. The put refuses a clone whose folder, `.git` or anything in `.git` is a link, owned by another user or writable by group or others, before the pull and again before the tag and the push; git runs with umask 022 so a fresh clone passes. A clone the work user owns must be removed once; the next put clones it again.

- `awb kb review` and `awb dataset put` (task k-dataset-signing, K2 of the review of 2026-10-20): `awb kb review` is refused for the work user. The review mark and `RELEASED.jsonl` count only when they are no link, belong to the user who runs the command and are writable by nobody else; otherwise the review or the put is refused in one line. A mark must name its own folder. Both files are written as new files of mode 0644 with `O_EXCL|O_NOFOLLOW`, never through a planted link. A `RELEASED.jsonl` an earlier put left group writable must be made the owner's alone once (`chmod go-w`).

- `awb kb review` and `awb dataset put --github` with signing (task k-dataset-signing, K3 of the review of 2026-10-20): the review mark pins the sha256 of every release asset (the zip, `HOW-TO.pdf`, `MANIFEST.json` and the `.mcpb`); the put refuses a mark whose assets differ from the folder now, and the signing compares each asset with the pin again before it writes `SHA256SUMS`, so the key vouches only for the files the owner reviewed. `SHA256SUMS` is written as a new file with `O_EXCL|O_NOFOLLOW`, never through a planted link. A mark written before this change has no pins: run `awb kb review` again.
- Key service (K4 of the review of 2026-10-20): the project of a session's call is the project whose folder the calling process works in (read from the peer process), no longer the `project` its request names. A session that names another project or works outside every active project folder, is refused with the line of a call without a project folder.
- The clean unit of a build task with `build_model = login` (task k-build-units, finding K22): a login file of the task that breaks the JSON parser (a deep nesting) no longer ends the unit before the home is removed. The unit treats it as not copied, removes the home and exits with the login-kept code.
- The login that goes back to the build user's home (task k-build-units, finding K8 of the review of 2026-10-20): only the token fields are copied, into the object that is there, so a key the task added stays behind; a login whose `expiresAt` is more than 48 hours ahead of the one there, or whose account id differs, is not copied and the run says so as before.

- `seal/verify.sh`: the check of the pdf extraction unit made its probe PDF with reportlab, which the runtime venv does not hold, so it failed on a real host although the unit works. The probe PDF is now a minimal one page PDF written with the standard library alone (`make_probe_pdf`), and the check reads "the unit of awb-extract runs pdftotext on a one page pdf". The `systemd-run` line is unchanged.

## Week 42, 2026-10-12 to 2026-10-18 (in progress)

### Added

- The build push mode `deployed` (task n14, D1): `build_push: deployed` in the host file makes the runner push `main` only up to the commit `/opt/tcp-awb/DEPLOYED` names as live, never further. When main.git is ahead the push stops at the deployed commit and the rest waits for the next retry (`awb build status` says `push: waits for the deploy of N commits`); when the deployed commit is no ancestor of main.git nothing is pushed. `yes` and `no` are as before and the default stays `yes`. The selftest gets the case `push-deployed`.

- Five lab recipes, tested on fake answers only (task n13): `eip-bandwidth-adjust`, `evs-snapshot-restore`, `nat-snat-egress`, `obs-lifecycle-rule` and `dns-private-zone`. Shapes the repository did not call before are marked to confirm live in the recipe notes and in `recipes/README.md`.

- The recipe `rds-create-refusal` after its live runs of 2026-10-17 on eu-de (two runs, nothing created): the default
  flavor is `rds.mssql.x1.large.2` (there are no `se` spec codes) and the volume type is the parameter `volume_type`
  with the default CLOUDSSD (ULTRAHIGH is unsupported in every eu-de AZ). The recipe format gets two small parts: a
  `preflight` list of reads made before the first create (the recipe reads the RDS storage types and stops with one
  line unless the type is normal in the AZ) and a `shape_error` rule on a create step, so a 400 with DBS.28xxxx stops
  the run as a recipe error and only the control matched 500 DBS.200005 counts as a refusal. `awb lab dry-run` shows
  the preflight as a step `0.`. `recipes/README.md` and the README describe both.

- `awb voice learn DRAFT - [--project CODE]` (owner side): the sent text comes on standard input, runs through `intake.run_text` in wipe mode for the customer of the project and is the sent text of the pair. Refused for the work user, in a session and from a terminal. A secret refuses the whole call. The two-file form stays for sessions.

- The deploy gate (threat model G2, `awb/deploy.py`): before a release is installed, the owner's plan child runs the
  commit gate (`awb gate`, the same checks a commit and a push get) over the commits from the deployed release to the
  target, the whole history when no release is deployed, and refuses on any finding with the commit and the class
  and never a value, so root installs nothing. A gate that cannot run (no register, git failed) refuses too. The
  verdict is a note of the plan, so `awb deploy --dry-run` shows `# gate: N commits a..b, clean` and a dry run of a
  refused target prints the plan, the refusal and no step, exit 2. `gate.range_commits` and `gate.scan_commits`
  (the commit part of the push scan, unchanged) are the pieces the deploy uses. The suite in the build unit and
  the diff question of the threat model's row are left open.
- `awb kb review [--out DIR] [--date D]` (gap G6 of the threat model F2, `awb/kbreview.py`): the facts of the
  newest built dataset (or the one of `--date`) added, amended (statement, source or grade) or retired since the last
  release, each with id, grade, check date, source and statement, codes only, compared with `RELEASED.jsonl` beside
  the folders (the `facts.jsonl` of the last release, which every successful `awb dataset put` writes). It writes
  `tcp-facts-<date>.review.json` beside the folder (the sha256 of the facts reviewed and the release compared with),
  and `awb dataset put` refuses a folder without that mark, with a mark of other facts or of another release, in one
  line that names the command. The put checks the review before the bucket, after the key service client is made.
- A project list per reader (gap G10, `awb/tcp/web/gateway.py`, `users.py`): `sudo awb web user projects LOGIN
  CODE...` stores the codes a login may open as `projects` in `users.json` (no code clears the list; a reset keeps
  it; `list` shows it). The gateway reads the list on every request: a route of a project not listed answers 404
  (`project` as the refusal word of the log line), `/api/projects` and `/api/board` carry the listed codes alone. An
  empty or missing list keeps today's behaviour of every project, so nothing changes before the owner fills it; an
  entry whose list is not a list of codes stays out like one with a bad level.

- Two small steps of the architect journey (F3, P8 and P13). The stop hook sends one mail through the key service's notify (`keys.send_mail`, as `awb build nudge` does) for every owner item of OPEN.md that names an `awb` command, once per item: the project code, the item and the command, never a name (`projects.mail_owner_commands`, the mark in `<code>.mailed` beside the ledger drafts, written only when the mail was sent). `awb close` prints the open items of OPEN.md and the live rows of RESOURCES.md, codes only, each with its command, when it refuses.

- `awb unlock`, `awb` alone and the lab defaults (P14, P15 and P7 of the F3 journey study): `awb unlock` runs `awb vault
  unlock` and `awb keys unlock` in turn and stops at the first that fails (the passphrases are asked on the terminal as
  before, never as arguments). `awb` without arguments prints the side (owner or work), the lock state of the vault and of
  the key service and the first owner item of OPEN.md, codes only (it exited 2 with the usage before). `awb lab run`
  takes `--project` from the `code:` line of the folder's SCOPE.md and `--tenant` from its `tenant:` line, which
  `awb spawn project --tenant ALIAS` writes; both flags still win. `README.md`, `docs/commands-guide.md` and `COMMANDS.md`
  follow.

- The reader service answers `GET /api/kb?q=`, `/api/price?service=` and `/api/usage` (Z3 step 10, server part,
  `awb/tcp/web/reader_api.py`): the knowledge base with the name check of today's register, the price records of one
  service and the TCP API calls of the month by day, as JSON with codes and counts only. The gateway forwards them
  to the reader service and they are in the contract (`docs/api/openapi.yaml`, state repository). The portal and
  the console pages are unchanged: the pages follow through the UI queue and the portal goes after the owner has
  seen them. `throttle.usage_days` is the count that `usage_lines` and the new route share.
- `COMMANDS.md` and the three client hook files are generated (Z3 step 12, `tools/gen_docs.py`): `COMMANDS.md` is the
  hand-written `docs/commands-guide.md` followed by a reference of every command and option taken from the argparse
  parsers, and `plugin/hooks/hooks.json`, `seal/work-claude/settings.json` and `seal/work-claude/managed-settings.json`
  are `hooks.client_settings` (the work user's file with its deny list). `python tools/gen_docs.py --write` writes them,
  without `--write` it lists what is out of date; `tests/test_generated_docs.py` fails when a committed file differs.
  The hook files did not change.

- The weekly read tier of the fact-check (design F9): `seal/work-jobs/awb-kb-recheck.service` and `.timer`, installed
  and enabled by `seal/setup.sh` beside the nightly jobs of the work user and proven by `seal/verify.sh`, run
  `awb kb recheck-run --tier read --tenant test-1` as the work user every Sunday at 02:30 UTC (up to 10 minutes later,
  no catch-up at boot), unix sockets only and after `awb-kb-commit.service`. The write tier never runs from a timer.

- `awb kb recheck-watch [--project CODE] [--tenant ALIAS]...` (fact-check design F5, `awb/tcp/recheckwatch.py`): the
  watchdog of the service project, run as the owner every 15 minutes by the root owned timer
  `awb-kb-recheck-watch.timer` that `seal/setup.sh` installs and `seal/verify.sh` proves enabled. It sweeps the
  tenants with the read key, deletes every resource of tcp-n7e6 whose `awb-expiry` has passed with the lab key (the
  recipe's teardown by its row in RESOURCES.md, else the delete call of its kind), proves each gone with a read and
  records it in RESOURCES.md. A resource it cannot delete is mailed once with its handle; every line of
  `<shared>/recheck/state.tsv` is mailed once. A resource of another project and one not expired are never touched.

- Three check recipes of the fact-check design F7: `ecs-flavor-boot` (any flavor the deny list leaves, the s9
  family too, after `cond:operation:az` says normal; the console read is reported, not failed), `smn-http-confirm`
  (an HTTP subscription confirmed by a plain GET of its subscribe_url) and `rds-create-refusal` (a SQL Server
  collation refused like an invented control, nothing made). The lab gains what they need: a `status` check on a
  create expects a refusal and the run goes on when nothing was made, a check may carry `report`, a gone proof
  `also` (a second read) and a list find `by` (the item field that holds the name).

- The refresh worklist carries the check block (fact-check design F8): every line of `worklist.jsonl` has `tier`
  (read, write, decision, none or `no block`) and `block`. An expired entry whose block a recheck run takes (read or
  write, needs none) names `awb kb recheck-run` and is that run's work; every other one, first of all one without a
  block, counts as overdue in the knowledge part, whose report adds a line with the count by tier.

- The board carries the recheck line (fact-check design F6): from `<shared>/recheck/state.tsv`, the newest run of each
  tier of `awb kb recheck-run` with its counts, the inventory of a write run and the entries for the owner
  (`recheck: read 2026-10-18 12 confirmed, 1 refuted, 3 undecided; write 2026-11-02 4 confirmed, inventory equal; 9
  for the owner`). A line whose date, tier or counts do not match is skipped, an exit or inventory cell outside the
  vocabulary is dropped.

- `awb kb recheck-run [--tier read|write] [--id KB-XXXX]... [--project CODE] [--tenant ALIAS] [--dry-run]` (fact-check
  design F4, `awb/tcp/recheck.py`): the expired entries with a check block of the tier, checked one at a time in the
  service project through the key service. A read tier sends its `GET` calls and reads confirms, refutes and control
  as tests `[N:]PATH is|is not|has|lacks VALUE`; a write tier runs its recipe as a lab run capped at 30 minutes, with
  the inventory of the sweep before and after. Confirmed is `kb recheck`, an event gets a line with the state of the
  day through `kb amend`, refuted and undecided stay expired (D2). Evidence in `evidence/recheck-<date>-<n>/`, one
  line per run in `<shared>/recheck/state.tsv`. Exit 4 for a resource left live or an inventory that differs.

- Named sources and id-set digests of the sweep (fact-check design F3): `sweep.NAMED_SOURCES` lists WAF policies,
  SMN topics and subscriptions and RDS instances (no tags the sweep reads) and finds the Workbench's own
  by the project code in their name (a subscription by the name of its topic). `sweep.inventory` gives per type the
  count and the sha256 digest of the sorted id set (unknown when a listing cannot be read) and `inventory_diff`
  names what differs in counts only (`rds +1/-0`), never an id. `awb cloud sweep` itself is unchanged.

- Recipe guards of the fact check (design F2): the flavor deny list `recipes/DENY.txt` (the GPU families p, pi, g,
  ai and kp, sizes above 2xlarge, physical and bare-metal flavors, database sizes from large.8) refuses a recipe
  that carries a denied flavor as a default, a choice, a var or a flavor field of a body and a run whose `--param`
  is one, before any call and by line number, never the value. A list that cannot be read refuses every recipe.
  `lab.capped` cuts a recipe to 30 minutes of life for a recheck run (`run(..., cap_minutes=30)`), the time budget
  with it. A password field may carry `{{random:password}}`: the key service draws a throw-away value at call time,
  masks it in the answer and never returns or logs it.

- The check block of a knowledge base entry (fact-check design F1): a line `--- check` after the statement, then
  `tier` (read, write, decision, none), `event`, `calls`, `confirms`, `refutes`, `control` and `needs`, written
  in that order. An entry without it parses as before. `awb kb how ID -` writes or replaces the block from standard
  input through the text checks of `add` (English, no name, no code, no price, no secret); `awb kb show` prints
  it and `awb kb verify` checks it. A block without `control` is refused, a write tier names a recipe of
  `recipes/`, a read tier sends `GET` or `HEAD` calls only and a block whose `needs` is not `none` takes the tier
  decision or none.

- `awb start project|query --goal "..." [--customer CUST-XXXX|new] [--tag TAG]...` starts a piece of work in one owner command (P1 of the journey report F3): it issues the customer code for `new`, asks the written forms at the prompt as `awb import` does, runs `awb spawn` as the work user through `sudo -n`, writes the grant of `awb projects kind`, imports the new files of the project's `in/` folder and ends with `open: awb projects open tcp-xxxx`. Four commands in two windows become one. `awb projects open` and `awb start` share the `sudo -n` check.

- `awb fetch tcp-xxxx [--again] [--into DIR]` on the owner side lists what a project put with `awb xchg put` since his last fetch, downloads it through the key service (the op `obs` list and get of `awb xchg list`, no new call) into `~/awb-fetch/tcp-xxxx/` of mode 700, puts the names back into every text with the code of `awb reveal` and prints the folder (P12 of the journey report F3). It replaces a console visit and an `awb reveal` per file.

- `awb projects open` without a code lists the active projects numbered, each with its `Next:` line and the owner items of its OPEN.md, after the name check (P4 of the journey report F3). He types the number and the session opens as `awb projects open CODE` opens it; without a terminal the list is printed only.

- `awb build nudge` reaches the owner when a build task waits on him (automation review A6): on 2026-10-13 a task
  asked at 23:15 and waited 5.5 hours while the steering session slept. For every task whose newest run is `asked`,
  `red: <reason>` or `held: <reason>` for 15 minutes with no answer, widen or retry since, it sends one mail through
  the runner's mail path whose body is his next commands (`awb build show ID` and `awb build answer ID "..."`; `awb
  build retry ID` and `awb build widen ID PATH...`; the hold's own command), never the agent's text. Once per state
  line of a run (the mark `nudged` in the run folder); a `nudged` row of `state.tsv` adds `nudged: B-013 asked
  23:30` to the board line. `seal/setup.sh` installs the root owned `awb-build-nudge.timer`, which runs it as the
  owner every 15 minutes in a hardened oneshot; `seal/verify.sh` proves the timer is enabled.
- `awb build draft FINDING [--from FILE] [--id B-NNN] [--out FILE]` writes the add file of a finding for `awb build
  add`, so the steering session does not write tasks by hand (automation review A4). It takes the row of a findings
  table (default `docs/review-2026-10-13.md`) and the section of its id: the title from the summary, `paths` from
  the place column, the module's test file and the document that marks the finding fixed (never an always allowed
  file), `check` the test file, `tests_replaced: none` with comment lines that list the rows of `build/tests.json`
  whose run names a touched test file. The prompt carries the finding, the defaults and the rules of a fix. It writes
  to standard output or a new file and never adds to the queue. `awb build add` skips comment lines `# ...` of a block.
- When the whole suite of a build task or of `awb build check` has failures, the failed nodes alone run once more
  in the same suite unit. When they pass, the check passes with a line `flaky: NODE...` in `checks.log` and
  `result.md`; a node flaky a second time in seven days adds a line to `build/queue/FLAKY.md`, which the steering
  session reads. The baseline and the hollow-test check never rerun.
- `awb build widen ID --tests-replaced ID...` adds test ids to the `tests_replaced` of a red task and the checks
  that run again read them. The ids go into the run's own `task.md`, the snapshot the runner's checks read (an edit
  of `QUEUE.md` never reached them); each widen adds a line to `widen.log` of the run folder. Paths are now
  optional when `--tests-replaced` is given.
- `awb pr take N` takes a pull request of a cloud session in one command instead of six steps (automation review,
  A8). In the owner's working copy on a clean `main` it fetches `pull/N/head`, rebases it onto `main` in a temporary
  worktree with hooks off, runs `awb build check` on the result and only when that passed merges it with `--no-ff`
  as "Merge pull request #N: TITLE", pushes `main` to origin and to `main.git` of the build and closes the pull
  request with gh and a comment that names the merge commit. A conflict in `build/tests.json` is merged by id and
  one in `CHANGELOG.md` keeps both sides' lines in their week, through the git merge drivers of `.gitattributes`
  (`awb/mergedriver.py`); two different rows with one id and any other conflict stop it with the file names. On a
  failure `main` stays as it was. It never deploys. `--dry-run` prints the steps.
- `awb build check REF` checks a branch from outside, a cloud pull request fetched into the working copy, as the
  build user and never in the owner's shell (F2 threat model, gap G1). The runner fetches the commit into the
  quarantine of `main.git` and runs its own checks on it: the commits after the merge base with `main` (at most
  200, none above 16 MB), the gate with the register over every commit message and changed file, the whole suite
  and the hollow-test check in the task's suite unit. It goes through the runner's lock, so it never runs beside a
  task, starts no model session and never merges, pushes or deploys. The result is `runs/C-001/1/result.md` with
  the new state `passed`; `awb build list`, `awb build status` and the board show it. `CLAUDE.md` names it as the
  only way to test such a branch.
- Ask runs on `claude-opus-5-5` by default. `AWB_ASK_EFFORT` (`low`, `medium`, `high`, `xhigh` or `max`, default `medium`) is sent as `output_config.effort` and a wrong value is refused when the service starts. `MAX_TOKENS` is 16000 (`AWB_ASK_MAX_TOKENS` changes it) because thinking counts against it; a stop at the limit gives a clear error instead of a cut answer. The answer shows text blocks only and thinking blocks go back to the API unchanged.
- The project chat and the Ask API (`awb.tcp.web.chat_service`, the service behind `awb-ask` on this host) send the same `output_config.effort` and `AWB_ASK_MAX_TOKENS` cap as the Ask page, fail a turn that stops at the cap instead of saving a cut answer and refuse to start on a bad `AWB_ASK_EFFORT`. `awb write translate` sends the effort too.
- The dataset TCP Facts installs in Claude Desktop with one click. Every build writes `tcp-facts-<date>.mcpb` (an MCP Bundle: a zip with `manifest.json`, the server and the data files of that build) beside the zip, and `awb dataset put --github` uploads it as a release asset. `README.md` of the build and the section "The dataset as an MCP server" start with the one-click install.
- Ask API for the console: `POST /api/ask` answers a question with the text, the fact ids it names, the prices it used and the questions left; `GET /api/ask/budget` returns the questions left. A refusal says why (`name`, `budget`, `busy`, `error`). It runs on the engine, the budget counter and the name check of the Ask page and follows the POST rules of the gateway; the owner host answers 404. In `docs/api/openapi.yaml`.
- `eval/tcp-facts/`: an evaluation set for the dataset TCP Facts. `questions.jsonl` holds 50 questions of the release 2026-10-12 (availability, limits, API behaviour, service scope, snapshot-bound prices and negatives with their evidence), each with the expected answer, the supporting fact ids and a grading rule. `score.py ANSWERS.jsonl` grades a file of answers and prints the score per category. `run.py --facts FILE` asks a model each question with and without `facts.md` when `ANTHROPIC_API_KEY` is set and does nothing otherwise. The README of the directory says how to run it.
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
- `awb register find WORD...` prints the code and the kind of the register entry whose form is these words (exact
  after the folding of the matcher: case, umlauts, whitespace) or `no code`, on the owner's own terminal only: refused
  with one fixed line in a pipe, as the work user and inside an assistant session, as `awb vault show` is; it never
  prints a form, a near miss or a count. `awb register add CODE KIND FORM...` takes several forms in one call and
  writes them in one go (one bad form and nothing is added); `-` reads them from standard input, one per line.

### Changed

- `awb web status` reads its health line from the gateway's status socket (`ok`, or `not installed` when there is no gateway) and no longer asks the removed portal on port 8180; `HEALTH_PORT` is gone (branch web-status-after-portal).

- The commands guide carries a list of the doors by kind of material.
- The forms prompt of `awb import` and `awb start` for a new customer asks for the German, the English and the Russian
  written forms (the full name, the short name, an acronym, the Cyrillic spelling), one per line.

- `awb bucket`, `awb import` and `awb dataset put` reach the owner bucket through the admin socket of the key service (Z3 step 11, `awb/bucket.py`, `awb/tcp/keys_exchange.py`), and `AWB_BUCKET_KEYS` and `bucket_keys` are gone: the bucket key stays in the key service. The service gains three admin calls, `bucket_list`, `bucket_get` and `bucket_put`, for the owner's user id alone: list in the folders of the layout, get from a project's `in/`, put into `out/` of an active project (and the folder marks) and into `datasets/tcp-facts/`, at most `max_mb`. Locked or stopped, every command stops with one line that names `awb keys unlock`. `awb bucket move` needs a copy and a delete that the service does not offer, so it stops with a line saying so.

- The intake as a package with the wipe rules by family (B-027, step 8 of the F1 plan), with no change of what it
  wipes, masks, holds or refuses: `awb/intake.py` keeps the run and is the door to `awb/intake/engine.py` (detection,
  tokens and replacement), `awb/intake/keep.py` (the keep list) and `awb/intake/wipe/`, the candidate rules in five
  families (`basic`, `lexicon`, `proposed`, `learned`, `runstate`). `awb.wipe` is that package under its old name and
  every old name of the intake still answers. The paste door (`awb import --paste`) runs through the new public
  `intake.run_text` and no longer drives the intake through its private names.

- The bucket layout and the owner routes in one place each (B-026, step 9 of the F1 plan), with no change of
  behaviour: `awb/bucket_layout.py` sets the folder `<YYYY-MM>/<code>/in/`, the default bucket and region, the 25
  MiB of the console's upload, the default 100 of `max_mb` and the suffix lists. `awb bucket`, the key service, the
  materials service and the exchange build on it. The gateway keeps its copy of the 25 MiB, held equal by a test. The owner
  routes are one table in the gateway (`OWNER_ROUTES`) that owner-actions reads too. A new test walks the gateway
  with every operation of the API contract on its host.

- The vault, the exchange, the project-root finders and the owner mail in fewer places (B-025, step 7 of the F1
  plan), with no change of behaviour: the client of the vault daemon is `awb/vault_client.py` and the sockets and
  the hand-over the key service shares are `awb/handover.py`; the service side of the exchange is
  `awb/tcp/keys_exchange.py`. Every old name still answers through `awb/vault.py` and `awb/tcp/xchg.py`. The hooks,
  `awb review`, `awb pricebook` and `awb write translate` find a project's root through one walk
  (`config.scope_root`); `awb ui watch` and the build runner send their mail through one `keys.send_mail`.
  `awb deploy` restarts for each new module what it restarted for its old one.

- The build runner and the client hooks in smaller modules (B-024, step 6 of the F1 plan), with no change of
  behaviour: `awb/buildrun.py` keeps the runner and its commands; its parts are `awb/build_units.py` (the host
  and the units), `awb/build_unit_side.py` (what runs in the units as the build user), `awb/build_checks.py` (the
  eleven checks), `awb/build_probe.py` and `awb/build_selftest.py`. `awb/hooks.py` keeps the hooks; the pre-tool
  guards are `awb/toolguard.py` and the receipt of a sealed start `awb/receipt.py`. Every old name still answers
  through the old module (`awb/parts.py`). `awb deploy` restarts for each new module what it restarted for its
  old one.

- The suite faster and less fragile (B-023, step 5 of the F1 plan): a test without a Workbench of its own gets a
  shared side, a vault, a knowledge base and throttle stamps of its own, no longer one set for the whole run. Run as
  root, the tests that need a user skip instead of failing: the dry runs of `seal/setup.sh` and `seal/verify.sh`,
  the code step of the seal and the root path of `awb deploy`. Every wait of a job goes through `jobs.pause`, the
  Retry-After of a busy gateway in the cloud client too, and the suite disarms it there. The runner tests copy one
  seeded world per module and the seal's code step one layout per run instead of building them for every test.

- One Ask engine (B-022, step 4 of the F1 plan): the model loop is written once, as `chat_service.engine`, and the
  project chat, `POST /api/ask`, the chat service's form page, the Ask page of a bare `awb-ask.service` and
  `awb ask QUESTION` all run it. Every door keeps its words, its answers and its source of names. `awb ask` and
  the bare page now work like the web Ask: an unreadable usage record refuses and every model round reserves its
  tokens first. The model's tool requests, the tool answers and the answer pass the screen: a tool answer with a
  hit is withheld from the model, an answer with a hit is refused.

- One text screen for the five doors (B-021, step 3 of the F1 plan): `check.screen(text, names, mask, allow)` finds
  registered names and structured data (also behind base64, hex and a bidi override) and the gate's secret, private
  key, token and home path detectors, and the Ask question, the project chat, the working copy of an imported
  material, the project read API (masks) and the owner status all call it. A door only chooses where its names come
  from and what it lets pass; the table "One text screen" in `INTERFACES.md` says which door refuses and which
  masks. No door accepts or shows more than before. Stricter now: the Ask question refuses secrets and home paths,
  the chat and the project read API see a name behind base64 or hex, and the project read API withholds a text with
  a secret.

- One definition of the code patterns (B-020, step 2 of the F1 plan): `awb/codes.py` spells the project, customer,
  fact and calculation codes once (`PROJECT_RE`, `CUST_RE`, `KB_RE`, `CALC_RE` and their text) and names the narrower
  or older shapes some modules accepted (`TCP_PROJECT`, `TCP_PROJECT_WIDE`, `ANY_PROJECT`, `KB_WIDE`, `KB_ANY_LENGTH`,
  `CALC_WIDE`). Every module of `awb/` builds its patterns on that text; `tests/test_codes.py` greps the package for a
  local spelling. The gateway imports no module of awb and keeps its own spellings, only the ones `codes.py` names.
  No pattern accepts more or less than before. The README counts six hooks everywhere.

- Dead and nearly dead code of the F1 review, section 2 (B-019, step 1 of the plan): `jobs.wait_gone` and its
  constants `PRESENT` and `GONE` are gone (no caller). The module text of `awb/cli.py` names the commands and points at
  `COMMANDS.md` for their subcommands and options instead of a stale list; `awb --help` names `kb index`,
  `kb verify` and `review calibrate`. `INTERFACES.md` documents the host-file keys `admin_socket` and
  `build_push_url` and fifteen `AWB_*` variables in one table. No behaviour change otherwise.

- The name check of the knowledge base accepts the full name of a TCP service from the service description (B-015).
  A fact about a service named like a customer was withheld and changing the word would change the fact. `awb kb`
  (add, amend, recheck, retire, verify, show, find, scope, expired, export) and so `awb dataset build` now drop a
  name hit that lies inside a name or short name of `rules/services.tsv`, as a whole phrase in the case written
  there and longer than the hit. The same word alone still hits; the intake and `awb gate` are unchanged.

- The held line of `awb inbox take` carries a class word that names no value (P2 of the journey report F3): `held (grant)` for a customer project the owner never granted, with the cure `awb projects kind tcp-xxxx query|project` instead of the import; `held (size)` above `max_mb`, which read as an error line before; `held (unreadable)` for a file the intake cannot read; `held (folder)` for a file in another customer's folder of the owner inbox. A project without a customer and a session refused `awb import` keep the plain line; the hold mail is unchanged.
- `rules/services.tsv` follows the service description of 09.10.2026 (`awb service update`): Savings plans is new
  as a preview at 3.12.7, Image Management (3.12.2) is gone and the other previews moved up one number (Cloud
  Container Instance 3.12.2 to Cloud Connect 3.12.6). 79 services as before. `rules/tags.txt` has the tag
  `savings-plans`; `rules/stop-words.txt` follows the list.
- The block "Customer data" of `README.md` and the warning of the dataset TCP Facts (its README, PROMPT.md and
  HOW-TO.pdf) say: give the model technical tasks only and keep them simple, since a detailed description of a
  customer's project can identify the customer without a name and that can break data protection law.
- The always allowed files of the build queue (`README.md`, `CHANGELOG.md`, `COMMANDS.md`, `INTERFACES.md`,
  `build/tests.json`) never reserve a path in the runner's pick or in `awb build list`, so a task that waits on a
  question no longer blocks the next task that lists them. `awb build add` warns (and still adds) when a block lists
  one.
- The build runner rebases a task's branch onto the `main` of now before the checks run, not only after they are
  green: after a session, after `awb build widen` and after `awb build retry`. `awb build check` puts a branch from
  outside onto the `main` of now the same way (`git merge-tree` in `main.git`). A stale baseline then never causes a
  false red; on 2026-10-14 a parametrized test whose id held a date failed the hollow-test check against
  yesterday's baseline. A conflict is `red: rebase conflict`.
- The MCP server of the dataset (`tcp_facts_mcp.py`) refuses a file outside the dataset folder with the JSON-RPC code
  -32050 instead of -32001, since the revision 2026-07-28 keeps new implementations off -32000 to -32019. The message
  stays the same (B-002, test MCP-23).
- The README, PROMPT.md and HOW-TO.pdf of the dataset TCP Facts say where the facts come from: how many of the
  documentation facts come from the repositories of the GitHub organisation opentelekomcloud-docs (Apache License
  2.0), that facts.jsonl holds the source of the others, that the documentation website is not crawled, that no
  community portal or internal documentation is used and that the live checks ran on the compiler's own test
  tenants. The licence section carries an attribution line for the Apache 2.0 sources (test DS-SRC1).
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

### Removed

- The read-only portal (Z3 step 10, the last part): `awb portal serve`, `awb/tcp/portal.py`, `awb-portal.service`
  and its drop-in `seal/web/awb-portal.service.d/90-awb-web.conf`, the gateway's forward of `/kb`, `/price`,
  `/projects`, `/reviews`, `/tenants`, `/health` and `/portal` (they answer 404 now; `/ask` stays) and
  `tests/test_portal.py`. The console shows the knowledge base, the price lookup and the usage on `/api/kb`,
  `/api/price` and `/api/usage`, which the contract (`docs/api/openapi.yaml`) now carries as deployed; the sign-in's
  `next` is `/` or `/ask`. `seal/setup.sh` (a seal and `--update`) stops and disables a left-over
  `awb-portal.service` and removes its unit file and drop-in folder, idempotent; `seal/verify.sh` proves the unit
  not found. The Ask page keeps the page template in `awb/tcp/ask.py`, with links to the console and to Ask. Not in
  the console yet: the tenant page's snapshot of what runs and ran per tenant (`awb tenant` prints it) and the
  portal's `/health` with `locked since` and the refused starts of today (`awb board`, `awb vault status` and the
  503 of `/api/board` carry them).

### Fixed

- The MCP Bundle of the dataset names its licence as the SPDX id `CC-BY-4.0` and carries `icon.png` (64 by 64, drawn with the
  standard library) named in the manifest's `icon` field. Q32 of the TCP Facts eval takes its expected date from the
  revision of the dataset under test (`run.py`), so a new release needs no hand edit.

- The send gate of `awb bucket put` no longer stops with a traceback when the owner cannot read the folder of
  another project (mode 700 from the work user's umask). It refuses with one line that names the project code and
  what to do, never a pass; a folder it cannot list under the projects root counts as a project. `awb close` writes
  a fingerprint of the deliverables to `<shared>/fingerprints/<code>.json` (sha256 and salted word hashes, no name
  and no text) and the gate checks a file against it for a closed project it cannot read, with the same verdict as
  reading the files.
- `awb kb recheck-run` without `--tenant` takes the tenant from `recheck_tenant` of the host file (B-018, FK9 fix):
  the weekly unit `awb-kb-recheck.service` named the tenant alias of the tests and would have stopped on the host
  every Sunday. The unit now names no tenant; `--tenant` still wins; with neither the run stops with one line that
  names the setting. `seal/verify.sh` warns once when the setting is missing or empty and `seal/setup.sh` keeps it
  when it rewrites the host file.

- `awb seal check` run as the work user passes "the build runner refuses its planted cases" again (finding SC):
  since the handover and rebase units of 2026-10-13 every case read `red: no commit`. The selftest runs its helpers
  in its own process, and the new clone check ran `git config` in the caller's folder; from a folder the work user
  cannot enter (the owner's home) git failed and the handover refused every clone. The selftest now runs with its
  throw-away folder as the working folder, as a unit has its own, and gives the caller's folder back after. Its
  cases are the same in both runs.
- `awb service update` reads a "No longer available from" mark again when the page footer of the PDF falls between
  the mark and the heading it belongs to: in the revision of 09.10.2026 Volume Backup Service lost its end date that
  way. A word in brackets with a run of three small letters, such as "GeminiDB (DynamoDB)", is no longer taken as a
  short name of the service.
- `awb dataset put --github` run by the owner no longer stops on git's "detected dubious ownership" for the clone
  `tcp-facts.git`, which the work user owns: every git call on the clone carries `-c safe.directory=<the clone>`,
  that path only and never `*`, so no global git setting is needed. A failed call names the git command, not the path.
- `sudo awb deploy` no longer pulls the home template out from under a running build task (automation review A1:
  two task runs of B-007 ended "Not logged in" a minute after a deploy). While an `awb-task-*` unit runs (or the
  task units cannot be listed) the deploy runs `setup.sh --update --no-build`, which leaves the whole build side
  out, writes `/opt/tcp-awb/BUILD-PENDING` and leaves `restart-wanted`. The next deploy without a task or the
  runner's next start (its unit runs `setup.sh --update --pending-build` first, as root) applies it and removes
  the file. `awb deploy status` and the plan name the pending step. `sudo awb deploy --wait-build` waits up to 60
  minutes for the task to end first; a task that outlasts it leaves the step pending and the deploy exits 1.
  `setup.sh` writes a new home template into a fresh folder and swaps it in with one exchange, so a unit keeps the
  files it bound; the folder of before stays as `.home-template.swap` until the next swap.
- The customer and project sockets (`awb.tcp.web.create_api`) read the declared request body (up to 2 MiB) before they answer a refusal, so a client that is still writing no longer meets a closed socket and a `BrokenPipeError`.

- `seal/verify.sh` names the runner's key copy once (`BUILD_MODEL_KEY`, as `setup.sh` does) and its login
  check reads that name. The test of the check gives it a file of its own: it failed on a host with
  `build_model = key`, because it looked at the host's real `/etc/awb/build-model.key`. It now also proves that a
  stray key copy fails the login mode.
- `awb cloud call` keeps a whole OBS answer (B-005: the live run of 2026-10-12 got the list of buckets cut after the
  first one at 300 characters). An OBS list in XML comes as data: ListAllMyBucketsResult as `buckets` (name, creation
  date, location when given), ListBucketResult as `keys` with `prefixes`, `truncated` and `next_marker`, Tagging as
  `tags`. Any other answer that is not JSON is still cut to its first 300 bytes; the answer says so (`cut`,
  `size`) and the command prints one line with the size of the whole. `awb cloud sweep --buckets` sweeps the OBS
  buckets of the region by the rules of the other types (untagged, expired by `awb-expiry`, not in RESOURCES.md), the
  tags from `GET /BUCKET?tagging`; without the flag the report says the buckets were not swept.
- The lab recipe `elb-health` tells a dead web server from a blocked health check (B-004: the live run of 2026-10-12
  left the member OFFLINE and could not say why). The backend's user_data writes the web server as a systemd service
  that outlives cloud-init and, after cloud-init, writes `AWB-WEB-UP` or `AWB-WEB-DOWN` to the console when it tries
  the port on 127.0.0.1. The new step `web` reads the console before the load balancer is made and the check
  `web-serves` wants `AWB-WEB-UP`; the rule for `100.125.0.0/16` stays, to confirm live.
- The key service signs a call to obs the OBS way (`Authorization: OBS AK:SIGNATURE` of `awb/obs.py`, the bucket
  by its own host, `GET /` the list of buckets) and every other service the way of the API gateway as before (B-003:
  the live run of `obs-policy-deny` on 2026-10-12 stopped at the bucket create with 400 InvalidArgument). An OBS call
  never reaches the two exchange buckets and an OBS write only a bucket named after its project. `awb cloud` and
  `awb lab` read the code and message of an OBS error in XML, so the evidence and the stop line carry them.
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
  ecs-admin is missing for the tenant`; the dry run names the secrets it needs. Both servers of `ecs-boot-ssh` carry
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
- An OBS listing that repeats its marker or runs past 100000 pages stops with an error instead of looping forever (review of 2026-10-13, X3).
- `tcp_facts_mcp.py --live` reads at most 32 MiB of the price API answer and falls back to the snapshot when it is longer (review of 2026-10-13, M3).
- `tcp_facts_mcp.py` reads a line from the client with a bound of 8 MiB: a longer line is answered with a parse error and skipped to its end, and the server goes on (review of 2026-10-13, M2).
- The egress filter accepts an established or related packet of the work user only with the conntrack mark the allow rules set (`0xa0b001`); every other one passes uncounted in count and is refused in enforce. A connection opened outside the list while the filter counted no longer survives `sudo awb egress enforce` and a listener of the work user no longer answers an inbound connection in enforce. The switch also deletes the conntrack entries of the work user's connections outside the list (mark `0xa0b002`) when `conntrack` is installed and says so when it is not. `seal/verify.sh` checks the two lines in the loaded chain (review of 2026-10-13, E1 and E2).
- `awb egress load` keeps the boot id in `status.json`. A table missing within the same boot (`systemctl restart nftables` flushes the ruleset) restarts `count_since` and `awb egress status` and `awb deploy status` say so (review of 2026-10-13, E3).

### Security

- The grant names its customer and needs it confirmed (task n20): `awb projects kind CODE KIND` for a project whose row names a customer writes the grant only with `--customer CODE` equal to the row's customer, because a session can write the row. Without the option it prints `grant not written` and the line to run, exit 1; another code is refused in one line. A project without a customer needs no option. `awb start` passes the code it registered. The grant file is unchanged in shape.

- A public push can follow the deploy: with `build_push: deployed` no commit reaches GitHub before the owner has deployed it, so the register gate over the pushed range runs on commits that went live first (D1).

- `awb dataset put` signs the release when the host file says `dataset_sign = yes` with `dataset_sign_key`: a signed tag (`git tag -s`, ssh format), `SHA256SUMS` and `SHA256SUMS.sig` as release assets, and a "check a download" block in the how-to, the README and the release notes (task n19).

- K1 closed (task n15): an OBS read (`GET`, `HEAD`, a listing) that a session makes through the key service is signed only in a bucket named after its project (`tcp-xxxx-...`), the list of the buckets and the tags of a bucket (the sweep) stay; any other bucket is refused with one fixed line. The lab bucket stays with `awb xchg`, which reads `<code>/in/` and `<code>/from-session/` of the project only. The owner's own calls are unchanged.

- The poppler tools of the pdf reader run in a transient systemd unit of the throwaway user `awb-extract` when paths.conf names `extract_user` (task n18-extract-sandbox): no network, read-only system, no home, 512M, 120 seconds, the file on standard input. A unit that fails fails that file and never falls back to direct; the import report and the intake counts carry `extractor: direct` or `extractor: unit`. `seal/setup.sh` creates the user and the sudoers line, `seal/verify.sh` probes it.

- A lab write allow list (gap G4 of the threat model F2, task n17): with `lab_writes = <service>:<path-prefix>[,...]` in `keys.conf` the key service signs a write of the lab role only when its service and path match one pair by prefix, else it refuses with one fixed line that names the service and never the path. The line is checked at the unlock (lower case service, `iam` never, a path that starts with `/` and has no `..`), `awb keys unlock` prints the count of pairs and `awb keys lab-writes --from-recipes` prints the line for every write of the recipes and of the recheck watch. Without the line nothing changes.

- The egress filter gets a second, counting chain for the Ask uid (task n16-egress-ask) in a table of its own (`inet awb_egress_ask`, `seal/egress/ask-egress.nft`) with a list of its own in `seal/egress/ALLOW.md` (the model endpoint and the price host) and a verdict word of its own, `ask_mode` of the host file (count by default). Everything else the Ask uid sends is counted under `ask_dns`, `ask_web` and `ask_other` and refused only in the mode enforce, set with `sudo awb egress --ask-mode enforce`. `awb egress status` and `awb egress count` print its counts as a block of their own. The work user's table and chain are unchanged.

- Four findings of the attacker review of 2026-10-16 of the fact-check service are closed (R3, R4, R5, R7). The recheck
  run never raises a grade: `write_back` passes the entry's own grade to `kb.recheck` and `pick` lists an entry whose
  grade is not live under "for the owner" instead of checking it, so a planted statement of the grade said with a
  well formed read block cannot come out of a Sunday run as live; `kb.block_problems` refuses a read tier whose
  confirms, refutes or control line tests `status` alone or whose confirms line equals its control line (R3). The lab
  judges the flavor deny list over expanded values too: the vars of a recipe when the run starts and the body of every
  call in `Run._call` before the send, so a flavor put together from pieces (`p2s.${size}`) is refused by its line
  number like a plain one (R4). The folder of a project is `<projects root>/<code>` and nothing else: `awb kb
  recheck-watch` and `awb lab` refuse a register row whose path is another folder (and a project folder that is a
  link) instead of reading and writing RESOURCES.md there, the watchdog's rows then go to the owner's state folder
  (R5). The leftover mail of the watchdog is built from the sweep's handle and kind, a row's type cell only when it is
  a plain type word, cut to the cap before the mask and printable ASCII like the exchange mails; a row's handle cell
  reaches the output, the mail and the row written back only when it reads as a handle (R7). Finding R8 (a tagged
  source without the project tag is the service project's by its name alone) is not changed: the lab names subnets
  and security groups because their services take no tag at create and two tests of the watchdog require the name
  path; the pull request names the patch for the owner.

- An assistant session of the owner's own uid gets no original from the bucket (attacker review, finding R6).
  Since the bucket went through the key service (8f661e9) the rule rested on the uid alone: a hijacked steering
  session could run `awb bucket pull CODE` while the service was unlocked. Now `awb bucket pull` and `awb bucket move`
  refuse inside an assistant session with one line that names your own terminal, the check `awb import`, `awb fetch`
  and `awb vault show` already make, and `KeyServiceClient.get` refuses a key under `in/` in a session before it
  opens the socket (so `bucket.fetch` with the real client gives a session nothing either). The service side cannot
  tell the peer's session, so the check stays on the client. `sync`, `folder` and `put` are as they were.
  `tests/test_bucket_session.py`.

- The web gateway no longer starves on slow bodies (n8, `awb/tcp/web/gateway.py`): a request without a session holds
  at most 4 of the 64 unauthenticated slots per address key (the /64 of an IPv6 address, an IPv4 address whole), the
  fifth answers 429 with the busy text before a byte of its body is read; the headers of any request and the body of
  a request without a session must arrive within 5 seconds (408, the connection closed for headers), a signed-in
  request keeps 20 seconds for its body. A refusal and a timeout are logged counted in the sign-in log as the
  refused tries are (`slots`, `slow`), one line per address key and minute, never the body. The owner host's own
  limiter is untouched. The contract (`docs/api/openapi.yaml`) says so.

- `awb pr take N` refuses a pull request that is not the owner's (attacker review R1, code part): before the fetch it
  asks `gh pr view N --json author,isCrossRepository,headRepositoryOwner` and `gh repo view --json owner` and stops
  with exit 2 when the head comes from another repository (a fork) or the author or the head's owner is not the
  owner of the repository; without gh and with a gh that gives no JSON the take is refused too, since GitHub serves
  `pull/N/head` for a fork as well and a commit's author field is set by whoever commits. A branch from outside may
  no longer change `seal/`, `plugin/hooks/`, `rules/`, `.claude/`, `CLAUDE.md` or `CLAUDE.local.md` unseen (R2):
  `awb build check` runs check 7 on a branch with that list the other way round and is `red: protected path` before
  any code runs, with a line that names the paths. `awb build check REF --allow-protected` and `awb pr take N
  --allow-protected` let them through, for the owner who read the diff of those paths first; the check line then
  names them. A link or a submodule in a branch is refused as it is in a task.

- Every unit of a build task gets `PrivateIPC=yes`, a `/dev/shm` of its own (`TemporaryFileSystem=/dev/shm:size=64M`)
  and `/dev/mqueue` inaccessible, so nothing a task leaves in shared memory outlives it or reaches the next task;
  `seal/verify.sh` runs its probe units with the same properties and checks that a unit sees an empty `/dev/shm` of
  at most 64 MB and no `/dev/mqueue`. The clean unit copies a login the task refreshed back to the build user's home
  only when it names the same `subscriptionType` as the login there; one that names another or none stays in the
  task home and `result.md` says so in one line; with `build_model = key` nothing goes back (review of 2026-10-13,
  findings B5 and B7).

- The work user can no longer persist past a session through its ssh keys or its crontab (F2 threat model, gap G3).
  `seal/setup.sh` copies the owner's keys to `/etc/ssh/authorized_keys/awb` (root, 644), writes the sshd drop-in
  `/etc/ssh/sshd_config.d/awb-work-user.conf` that points the work user alone at that file, checks it with `sshd -t`
  before the reload (a refused drop-in is removed again) and removes `<work-home>/.ssh/authorized_keys`. The two
  nightly jobs of the crontab (`claude update` at 01:35 into `.local/state/claude-update.log`, the commit of the
  knowledge base at 01:40) become the root owned timers `awb-work-update.timer` and `awb-kb-commit.timer` with hardened
  oneshot services that run as the work user (`seal/work-jobs/`); `/etc/cron.deny` names `awb` and its crontab goes.
  The kb commit script is not in the repository, so it still runs from `<work-home>/.local/bin/` and the setup says
  so. `seal/verify.sh` proves the key file, the file `sshd -T -C user=awb` names, `cron.deny`, a refused
  `crontab -l` as the work user and both timers. First seal only: a deploy does not touch them, so a sealed host
  gets them from one more `sudo seal/setup.sh`.
- A build task's session unit no longer reaches a nameserver (review of 2026-10-13, finding B4). `IPAddressAllow=`
  takes no port, so the upstream nameservers in its allow list were open on every port and protocol: a channel
  through DNS for the login, the key or the clone and, where the resolver is the metadata address, the metadata API.
  The runner still resolves the model endpoint and the token host when the task starts, allows those addresses alone
  and binds a hosts file of loopback and those names over `/etc/hosts` and a `resolv.conf` without a nameserver over
  `/etc/resolv.conf`. `awb build probe` binds the same, its hosts file also names the TCP API host and GitHub; the
  probe units of `seal/verify.sh` bind a hosts file of loopback and a `resolv.conf` without a nameserver.
- The build runner sizes every blob a task's commits add or change and every commit with `git cat-file
  --batch-check` before it reads any of them; one above 16 MB is `red: blob too big` or `red: message too big` at
  check 4 and the push refuses such a range. `awb-buildd.service` has `MemoryMax=2G`. Before, a blob of many GB of
  zeros fit in a small bundle and the owner's runner read it whole, so the OOM killer could take the vault daemon or
  the key service (review of 2026-10-13, finding B3).
- No test can reach the network any more (finding T1 of `docs/review-2026-10-13.md`). The audit hook of `tests/conftest.py` refuses a socket connect (tuple addresses too) and a `getaddrinfo` or `gethostbyname` to any host that is not loopback while a test runs. The test fails even when the code under test caught the refusal. No test of the suite needed a fake: none reached a host outside this machine.
- The tests no longer reach the real home or the key socket when a test patches `config.is_work_user` (finding T3 of `docs/review-2026-10-13.md`). `HOME` is a folder of the test and the guard of `tests/conftest.py` refuses every path under `/run/awb-` (the key service socket among them).
- The tests keep the real `HOME` again (owner decision T3H-a on finding T3 of `docs/review-2026-10-13.md`). The repository and its `.venv` live under the real home, so with a moved `HOME` the spawned projects carried absolute home paths and the gate refused them (four tests failed on this host). Instead the guard of `tests/conftest.py` refuses the real `~/tcp-shared` and `~/tcp-kb`, the shared and kb folders of the root host file and the `AWB_SHARED` and `AWB_KB` of the start of the run. A test that patches `config.is_work_user` and reaches one of them fails, even when the code caught the refusal.
- The guard of `tests/conftest.py` is on from the import of that file, before any test module is imported and before any fixture runs, so the import-time code of a test module and a fixture of module or session scope meet it; a refusal recorded before a test errors the first test that follows at its setup (finding T4 of `docs/review-2026-10-13.md`). The guard also compares the real path of every path it sees, so a link planted before the run into a temporary folder that points at a vault root is refused like the root itself; `os.path.realpath` follows the links of the parents of a file not made yet and opens nothing (finding T5). `tests/test_vault_guard.py` proves both on a temporary folder standing in for the vault root.
- The tests no longer see the `AWB_*` variables of the owner's shell (finding T2 of `docs/review-2026-10-13.md`). The autouse fixture of `tests/conftest.py` deletes every one of them first and then sets the invented ones. It also puts a `pass` that exits 1 first on `PATH`. With `AWB_BUCKET_KEYS=pass:ENTRY` exported, a test could run `pass show` on the real store and reach the real OBS.
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
- The MCP server of TCP Facts answers a message nested deeper than the JSON parser goes with a parse error and
  goes on; before, such a line ended the server (review of 2026-10-13, finding M1).
- An OBS answer that declares a DOCTYPE or an entity is never parsed: `awb cloud call` and the key service take
  it as plain text, an OBS listing refuses it. An object body read through the key service could carry one and grow
  in the owner's daemon (review of 2026-10-13, finding X1).
- `awb cloud call` and the key service read an answer up to 16 MiB and leave the rest unread; the answer is cut
  and the command says it was larger. Before, a GET of a large object of a project bucket was read whole into the
  owner's key service (review of 2026-10-13, finding X2).
- A build task's unit hides only the folders of `wt/` whose names the runner makes (`B-NNN-N`, `B-NNN-N.out`). A
  folder named with a quote or a blank, made by a task, could split or join the unquoted path list of
  `InaccessiblePaths=` and so unhide the clones of other tasks (review of 2026-10-13, finding B2).
- The export of a build task runs no filter and changes no file: `info/attributes` of `main.git` also switches
  `filter`, `ident`, `text`, `eol` and `working-tree-encoding` off. A `.gitattributes` of a task could turn on a
  filter of the owner's git configuration (run as the owner) and make the export differ from the blobs the gate read
  (review of 2026-10-13, finding B6).
- The handover and the rebase unit of a build task write only the task's clone and out folder, see no other clone
  or home, no login and no record and run in a network namespace of their own; only the prepare and the clean unit
  keep `wt/` and `home/`. Before git runs there, a clone whose `.git` is not a folder or holds `commondir`,
  `config.worktree` or `info/attributes`, or whose `.git/config` holds a key outside a short fixed list (a filter, a
  driver, `gpg.program`, an include), is refused with exit 4, and git runs with fixed overrides (fsmonitor, hooks,
  attributes file, signing off). A session could plant a command in its clone's git configuration that ran in the
  unit with every clone and home in reach (review of 2026-10-13, finding B1).

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
