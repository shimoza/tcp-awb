# Install, the test suite, the laptop edition and the plugin

The Workbench without the sealed host. The sealed host itself is in [INSTALL.md](../INSTALL.md). Back to the [README](../README.md).

## Install

You need a Linux host (tested on Ubuntu 24.04 with Python 3.12), `git`, `gpg`, the poppler tools (`pdftotext`,
`pdfinfo`, `pdfimages`, `pdfdetach`) and Claude Code with your own subscription login. The `awb` command itself
needs no model and no API key. Runtime code is the Python standard library plus the poppler tools.

```bash
git clone https://github.com/shimoza/tcp-awb.git ~/tcp-awb
cd ~/tcp-awb
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
.venv/bin/awb gate --selftest
```

The tests use invented names only (`tests/fixtures.py`) and a throw-away Workbench each. They never touch your own
folders or the sockets of the services on the host, so two runs at the same time do not wait on each other. Every
test fails after 120 seconds (pytest-timeout) instead of hanging, also when it blocks again in its teardown
(`tests/timeouts.py`); a test that never comes back to Python ends the run with the stacks of every thread 30 seconds
later.

The suite is quick where it can be: the stand-in servers shut down at once; the waits of a job (`jobs.pause`: the
retry waits of the fetch code and the Retry-After of a busy gateway) are not waited out; the self-test of the intake
runs once for an untouched engine; the runner tests copy one seeded world per module. Each test gets a shared side, a
vault, a knowledge base and throttle stamps of its own, so nothing one test leaves there reaches the next. Run as
root (a container), the tests that need a user skip: the seal scripts refuse their test-only variables as root and
`awb deploy` refuses root as the owner. A few tests must stay long and carry
the marker `long` with the reason on the test; `.venv/bin/pytest -m "not long"` leaves them out for a quick look in a
working loop, the full run and every build run them. Running with pytest-xdist (`-n 2`) is not set up: some tests
rely on the timing of real daemons and failed under the load of a second worker. The build's suite unit runs the
whole suite as one pytest per test file instead, four files at a time, each with a HOME and a TMPDIR of its own
(Q-0013). A file with its own process pool and every test marked `wallclock` (it asserts how long something took)
run alone before that queue; `tests/test_suite_wallclock.py` fails when a wall-clock bound has no marker (Q-0028).

The suite also runs from a read-only copy of the tree anywhere outside the homes, with a venv that has the test
extras and not the package, as the self-build runs it on a `git archive` export:

```bash
seal/setup.sh --suite-venv /var/tmp/awb-suite-venv     # python3 -m venv --system-site-packages and the test extras
cd /path/of/the/export && PYTHONDONTWRITEBYTECODE=1 /var/tmp/awb-suite-venv/bin/python -I -B -m pytest
```

The self-build also switches the cache plugin off (`no:cacheprovider`), so the run writes nothing next to the tree.

When the interpreter does not import this tree by itself, `tests/conftest.py` gives the children of the tests a
throw-away interpreter that does (one `.pth` line in a temporary folder), and a copy a test makes of the tree is
writable for it; nothing is written into the tree.

### On a laptop

For the partner who wants the Workbench on a laptop without the sealed host, install `awb` as above, run `awb init`,
then `awb kb import facts.jsonl` from the latest TCP Facts release (github.com/shimoza/tcp-facts; the zip of the
release works too). The new knowledge base starts empty and the import fills it: every fact is checked as `awb kb add`
checks it (the form, the language and the price; no name check), an id that is already there is skipped and the counts say what was imported, skipped and refused. It
needs no vault daemon, no key service, no register and no work user. Run it again with `--update` after a newer release.

### Clean material on your laptop first (`awb clean`)

The anonymization of customer material can run on your own laptop (Ubuntu under WSL on Windows 11), so the host gets
clean files only and imports them in seconds. The same laptop edition: no vault daemon, no key service, no work user.
`awb init --laptop` (or `awb clean init`) makes `~/awb-laptop` (`AWB_LAPTOP_HOME` names another folder) with its
vault folder (700). `awb clean shell` then puts the PATH line of `~/.local/bin` and the source line of
`~/.config/winrelay.sh` into `~/.bashrc` once and, with oh-my-zsh, into
`${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/awb.zsh`; `~/.zshrc` is never touched, since oh-my-zsh replaces it on
install. `awb clean register CUST-XXXX` asks the written forms of the customer once on the terminal, as
`awb import` does; they stay in that vault folder and nowhere else.

    awb clean use tcp-xxxx
    awb clean FILE... [--project tcp-xxxx] [--kind docs|logs] [--out DIR] [--put] [--llm [URL]]
    awb clean put
    awb clean teach keep WORD | wipe WORD [--kind person|company|place] | list
    awb clean measure [--llm [URL]] [--only MODULE ...]

- Name the project once with `awb clean use tcp-xxxx`: it is remembered on the laptop and its work folder
  `~/work/tcp-xxxx` is made (with a layout root, `clean/` of `<root>/YYYY-MM/tcp-xxxx/`, section "Folder layout and
  the move plan" below). A run takes `--project` first, then the work folder of the current directory, then
  that project, and prints it in its first line. Without `--project` the copies go into `clean/` of the work
  folder. `--kind` defaults to logs for a `.log` file and to docs for any other. A Windows path
  (`D:\Documents\x.docx`, `D:/Documents/x.docx`) is read as `/mnt/d/Documents/x.docx`. A customer that is not
  registered yet is asked for once on the terminal. `awb clean put` sends the copies of the last run without
  cleaning again (`--put` on the run still works).
- `awb clean serve [--port 8765]` is the same flow as a local page on 127.0.0.1: open the printed
  `http://localhost:8765/?t=TOKEN` in the Windows browser, upload or type the files, run, read the report, open the
  copies as text and put them. The token is new per start, another Host header is refused, uploaded originals are
  deleted after the run. Load `AWB_OBS_AK` and `AWB_OBS_SK` before the start for put.

- `--kind docs` for customer documentation (docx, pptx, xlsx, odt, pdf, also Markdown, text, html and mail files):
  the body text only. Pictures (logos), document properties, headers, footers, comments and tracked changes are read
  for detection and never written. The customer's forms become `CUST-XXXX`, persons, companies, places, mail
  addresses and phones become tokens by the rules of wipe mode, secrets `[secret]` (keys, tokens, password
  fields and a password written in prose after a password word, in English, German or Russian). One
  `<name>.clean.md` per file.
- `--kind logs` for the dump of a technical problem (logs, configs, tickets: text, log, csv, json, yaml, conf):
  mail addresses, phone numbers, user names (user fields, sshd and PAM lines, DN, `-u`, home folders), IP addresses,
  internal host names and FQDNs that are no public product or service host, keys, tokens and passwords are wiped.
  No name is guessed from the shape of a word, so services, errors, versions, product names and stack traces stay.
  One `<name>.clean.<suffix>` per file.
- The report `clean-report-<stamp>.md` in the output folder gives kinds and counts per file, never a value. The
  mapping (token, class, value) of each run goes into `~/awb-laptop/vault/maps/tcp-xxxx/` and stays there.
- `--llm [URL]` adds a second pass by a local model to a docs run, for free text such as a meeting transcript,
  where the rules miss names. The rules run first and unchanged. Then the cleaned text goes in pieces of about 2,000
  characters, cut at a sentence, to the model, which is asked for the spans that are still names of persons,
  companies or places. A span is taken only when it stands in its piece literally and is no entry of the lists of
  public words, technical terms and allowed terms (`rules/*.txt`) and no keep word of yours; it becomes a token like
  any other and goes into the mapping. The model never rewrites the text. The URL defaults to the GPT4All API,
  `http://localhost:4891/v1/chat/completions` (`AWB_CLEAN_LLM_URL` names another, `AWB_CLEAN_LLM_MODEL` the model
  as GPT4All names it, default `Llama 3 8B Instruct`, `AWB_CLEAN_LLM_TIMEOUT` another limit than 120 s per piece). It must be loopback (127.0.0.1, ::1 or localhost):
  any other host is refused with one line before anything is sent; the call never goes through a proxy. A model
  that does not answer, times out or answers no valid JSON costs only that piece: the run goes on with the rules
  alone. The report gets a section with one line per file: pieces sent and missed, spans proposed, taken, dropped as
  public words and not in the piece, by kind, counts only. A logs run gets no model pass. Start the API in GPT4All
  under Settings, Enable Local API Server, with a model loaded; a CPU-only 7-8B model may need minutes per
  transcript. No model is trained or fine-tuned, here or anywhere: the model is used as it ships. What gets better
  is the rules, the word lists and your corrections.
- `awb clean teach keep WORD` keeps a word the wipe replaced (the word or the phrase as it stood) and `awb clean
  teach wipe WORD [--kind person|company|place]` wipes a name that got through (person by default). They go to
  `~/awb-laptop/vault/teach.tsv` (600) and every later run on the laptop reads them: a keep word is never wiped, a
  wipe word is matched like a registered form and becomes a token of its kind. `awb clean teach list` prints the
  counts per kind. Nothing of the file goes to the bucket, a report or an update.
- `awb clean measure [--llm [URL]]` runs the wipe pack (`calibration/redteam/wipe/` of the clone, invented cases
  only) with the rules alone and with `--llm` also with the model. It prints the cases that leak and lose per case
  module for both, so you see on the laptop whether the model helps. `--only meetings` runs the 40 meeting
  transcripts alone (with the rules alone 17 leak and 1 loses, see `calibration/redteam/EXPECTED.md`).
- Nothing in this path opens a network connection to another machine (`--llm` talks to the loopback address
  only). `--put` is the one exception: the clean copies (never the
  originals) go into `<YYYY-MM>/tcp-xxxx/in/` of the bucket `awb` (eu-de) with your own key from `AWB_OBS_AK` and
  `AWB_OBS_SK` (`AWB_OBS_BUCKET` and `AWB_OBS_REGION` name another bucket or region). The folder is looked for
  under every month and created under this month when missing.
- `awb clean update` fetches the newest lists of public words (rules/common-words.txt, tech-terms.txt,
  allowed-terms.txt, allow-domains.txt and the other allow lists) from github.com/shimoza/tcp-awb: a
  `git pull --ff-only` of the clone, or for an install without git a download checked against the blob ids of the
  repository tree. It sends nothing of your files and prints what changed in counts and the version before and
  after. It never touches local changes: a clone with changed files or commits of its own is refused by the
  fast-forward rule, says so and stays on its version.
- The version line: on a terminal every `awb clean ...` command prints first `awb clean <short commit> (<commit
  date>)`, from the HEAD of the clone or, for an install without git, from the `rules/VERSION` stamp the HTTPS
  update writes. `awb clean doctor` ends with how many commits the install is behind the public main.
- It updates itself at two moments you already have. `awb clean shell` adds a block that starts
  `awb clean update --boot` at the first shell after a WSL boot: once per boot (the boot id against
  `~/awb-laptop/boot-update.id`), in the background under `timeout 60`, never in the way of the prompt, its output
  in `~/awb-laptop/update.log`. The next `awb clean` command prints once what it did: `boot update: updated
  23dc2c9 -> 3388b32` or `boot update: update failed: <reason>, still on 23dc2c9`. And `awb clean apt-hook` prints
  the one sudo command that writes `/etc/apt/apt.conf.d/99awb-clean`: after `sudo apt update` it runs
  `awb clean update --quiet` as your user (never as root), with the proxy of `~/.config/winrelay.sh`, and never
  fails apt. `awb clean apt-hook --check` says whether it is installed. Once on the laptop:

  ```bash
  awb clean shell        # then open a new terminal
  awb clean apt-hook     # prints one sudo command: run it, then awb clean apt-hook --check
  ```
- `awb clean fetch tcp-xxxx [--to DIR] [--save] [--again NAME]` brings a finished deliverable to the laptop. On the host `awb bucket put CODE FILE --reveal` puts the file with the real names into `out/` (a Markdown file as `.md` and `.html`, the HTML to paste into a mail) and ends with `get it: Total Commander, bucket awb, folder <YYYY-MM>/tcp-xxxx/out/, or on the laptop: awb clean fetch tcp-xxxx`. On the laptop the command lists only `out/` of the project's bucket folder with your key, downloads the files not fetched yet into DIR (`--save` keeps DIR as the default, for example a folder under `/mnt/c/Users/<you>/`) and prints one line per file, for an HTML file also its Windows path to open. `--to C:\Users\<you>\Downloads` is read as `/mnt/c/Users/<you>/Downloads`. What was fetched is remembered by etag in `~/awb-laptop/vault/fetched.tsv` and not fetched again; `--again NAME` (the file name in `out/`, repeatable) fetches it again and keeps a local file of that name, the new one gets ` (2)`. `in/` is never touched.
- `awb clean report [--dir DIR] [--save]` brings the codes file of the report workbook to the laptop (section "Report workbook" below).
- `awb clean inventory ROOT... [--project tcp-xxxx] [--label LABEL] [--put]` lists the files under the folders you name without their content, for the move plan: the full list with the real paths stays in `~/awb-laptop/vault/inventory/` (600), the coded copy (paths coded through the same name machinery, `[withheld]` where one still carries a name, sizes and hashes) is what `--put` sends to `reports/inventory/` of the bucket. The report folder and the report workbook are skipped, a root under OneDrive needs `--include-onedrive`.
- Behind a corporate proxy `--put`, `awb clean fetch`, `awb clean report`, `awb clean inventory --put` and `awb clean update` use `HTTPS_PROXY` and `https_proxy` (git reads them too).
  `awb clean doctor` prints the proxy variables (host and port only, never a user or a password), sends one HEAD
  request through the proxy to the object storage of the region and to github.com and names the fix when one
  fails: the proxy cannot be reached, it wants a login, or TLS verification failed (a TLS-inspecting proxy: its root
  certificate through `update-ca-certificates`, then `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`). With
  zsh and oh-my-zsh but no `awb.zsh` it names `awb clean shell`, with `HTTPS_PROXY` unset while
  `~/.config/winrelay.sh` exists it names `. ~/.config/winrelay.sh`.

On the host the timer `awb-import-auto` (seal/setup.sh, every 2 minutes) runs `awb import --auto`: the new files of
`in/` of every active project come in, a clean copy with `--trusted` (no name guessed, the registered forms, the
secret wipe, mail addresses and phones still apply) and any other file in wipe mode. Every import runs the secret
wipe of `awb clean` before the names: keys, tokens, password fields and passwords in prose become `[secret]`. The original moves into the
vault and is deleted from `in/`, the session of the project reads "new input: N files (FILE IDS)" at its next prompt, a project whose import runs is skipped and each run
writes one line to the journal and to `~/tcp-vault/import-auto.log`.

Every import copy, also with `--trusted` and `--redo`, has its home paths replaced by `[path]` and its blocklist words by `[withheld]`, so the commit gate accepts the copies; the import ends with the gate's check on them: `N copies, gate clean` or the classes left.

### Report workbook

The weekly status per project lives in his own workbook `report.xlsx` on the work laptop (sheets Projects, Names and
Status), in `C:\Users\<you>\awb-report\`. The Workbench writes only the codes file beside it, `projects.csv` (six
columns, codes only) and `projects.previous.csv`; no command opens or writes the workbook. Excel links to the codes
file with Power Query and the Names sheet turns the customer codes into names on the laptop alone.

    awb clean report --dir "C:\Users\<you>\awb-report" --save      # once
    awb clean report                                                 # each time

It downloads `reports/projects.csv` and `reports/projects.meta.json` of the bucket with your key through the proxy,
checks the sha256, keeps the file before and replaces the new one in one step with three tries while Excel holds it.
It never uploads.

The same command brings the KPI file `projects-kpi.csv` (and keeps `projects-kpi.previous.csv`) for the reports and
the half-year review: every project since 2026-09-01, closed ones too, ten columns (project code, customer code,
kind, goal, start date, close date, state, tags and technologies, ledger entries, main deliverables), codes only.
The host writes it beside `projects.csv` by the same route (`awb report --codes`, work side to the shared side, the
owner's hourly put to `reports/`). To link it in report.xlsx: Data > From Text/CSV, pick `projects-kpi.csv` in the
report folder and load it as a new query on a sheet of its own; `projects.csv` and its query stay as they are.

The report workbook never leaves the laptop (`rules/report-workbook.txt`): `awb clean`, `--put`, `awb clean put` and
the page refuse a file in the report folder and any workbook with both sheets Names and Projects, `awb clean fetch
--to` refuses the report folder; on the host `awb import` and its timer hold such a workbook, import nothing of
it and mail you to delete it from `in/`.

### Folder layout and the move plan

One layout root on the laptop holds every project as `YYYY-MM/tcp-xxxx/` with `private/` (originals and your notes),
`clean/` (what went up), `out/` (deliverables fetched back) and `_archive/` (what the move plan moved aside).

    awb clean layout "D:\Work"                 # once; --save replaces it later
    awb clean use tcp-xxxx                      # makes private/, clean/ and out/; clean/ is the work folder
    awb clean files plan                        # the newest plan of the host, with your real paths
    awb clean files apply                       # the dry run: what would move and what is skipped
    awb clean files apply --go                  # the moves
    awb clean files undo <stamp>                # everything of that run back

The month of a new project folder is `--month YYYY-MM`, else that of its bucket folder when your key is set, else
this month. `awb clean fetch` without `--to` lands in `out/`. The plan comes from `reports/plan/<stamp>/PLAN.tsv`
of the bucket (ids and coded paths only); the laptop joins it with the full local inventory of `awb clean inventory`
and prints the real paths on your screen only. The apply never deletes and never overwrites (a taken name gets
` (2)`), checks the sha256 of each file against the inventory and skips a changed or missing one. It never touches
the report folder, the report workbook, OneDrive without `--include-onedrive` or anything outside the inventory roots
and the layout root. Each move goes into the undo journal `~/awb-laptop/vault/moves/<stamp>.tsv` (600).

### Tidying the buckets

`awb files plan` proposes an action for every object of the two buckets; `awb files tidy` carries them out, in your
own terminal on the host (never as the work user, never inside an assistant session) and never without a `yes`.

    awb files plan                                                  # ~/tcp-shared/files/plan-<stamp>/
    awb files tidy --go ~/tcp-shared/files/plan-<stamp>/ --dry-run    # what it would do; nothing changes
    awb files tidy --go ~/tcp-shared/files/plan-<stamp>/              # the same list, then type yes

Strike a row you do not want by deleting its line from `PLAN.tsv` before the run; the command never adds a row and
takes only a plan made in the last 24 hours (else: make a new plan). `--only owner-bucket` or `--only lab-bucket`
and `--project tcp-xxxx` narrow the rows. `keep` and `withheld` do nothing. `move`, `unsorted` and `archive` copy the
object to its target, compare size and md5 (the size alone when the etag is multipart, which the log says), then
delete the source; a target that exists is skipped, a copy that differs leaves the source in place. `delete` deletes
only when the keeper the row names is there and identical or for `stale-in` when the vault's record of the imports
still holds the key and md5; a delete without a keeper is refused. Every row is checked against the bucket first: an
object that is gone or changed its size since the plan is skipped. Each row goes into
`~/tcp-shared/files/tidy-<stamp>/LOG.tsv` (id, action, source, target, ok, skipped or failed, reason), which also
goes up to `reports/tidy/<stamp>/LOG.tsv` of the owner bucket. One failed row never stops the others; the exit code is 1 when
one failed. The key service has no copy and no delete, so the tidy reads the key it uses for both buckets (the lab
key of `bucket_tenant` in keys.conf) from your password store at call time and keeps nothing.

After an archive of a lab put nothing else is needed: INDEX.md of the project drops the rows whose object is gone on
the next put.

#### The archive without a plan

An archive is a move inside the bucket and never a delete of content, so the two archive moves of the plan also
happen where they arise, without a plan (D-B1). Both copy, compare size and md5 as the tidy does and delete the
source only after that; a move that fails leaves the source where it was.

- `awb fetch tcp-xxxx`, after the download: every put whose INDEX.md row says fetched 30 days or more before today
  moves to `tcp-xxxx/_archive/<YYYY-MM of the put>/from-session/<date>/<kind>/<key>` of the lab bucket, the HTML
  page of a Markdown put with it, and its row leaves INDEX.md. The key service does it (the op `obs` ARCHIVE, your
  processes only). One line says `archived N put(s) fetched 30 days or more ago` and counts a failed move, which never
  stops the fetch. `--no-archive` skips it.
- `awb bucket put tcp-xxxx FILE`: when `out/` holds an older version of the same group (project, normalised stem,
  suffix, as the plan groups: `offer v1.md` for `offer v2.md`, its HTML page with it), the older one moves to
  `<YYYY-MM>/tcp-xxxx/_archive/<stamp>/<name>` first (the key service, op `bucket_archive`), then the new one is
  put. A file with the size and md5 of one of its group in `out/` is not put again; one line says so. `--replace`
  still means the same name only: that file is overwritten, not archived.

Nothing is deleted on the work side, and the key service never deletes a file of `out/` or `from-session/` without its
compared copy in `_archive/`.

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
| hooks | seven checkpoints: every prompt, before a write, before every file or shell call (the session stays in its project), after a write, the start and the end of a session and the compaction (the pinned block of `STATE.md`) |

On a sealed host ([operations.md](operations.md#the-sealed-setup-and-its-operation)) the seal installs the hooks for the work user already. Do not install the plugin there as
well: every hook would run twice.
