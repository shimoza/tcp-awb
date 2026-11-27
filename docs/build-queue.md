# The build queue

How the Workbench builds itself from a queue of tasks. Back to the [README](../README.md).

## The build queue

The Workbench builds itself from a queue (`presentations/self-build`). A task is one block in
`build/queue/QUEUE.md` of your working copy and a prompt file beside it; the folder is untracked. Write a file with
the block, a line `---` and the prompt, and add it:

```
## B-012: the take from in/ checks the size before the get
depends: none
paths: awb/tcp/xchg.py tests/test_xchg.py
tests_replaced: none
check: tests/test_xchg.py -k size
---
Make the take check the size of a file before the get. ...
```

```bash
awb build draft E1 --out task.md   # an add file from finding E1 of docs/review-2026-10-13.md, never queued
awb build add task.md        # checked (the grammar and the size, no content check), then written atomically
awb build list               # id, state, since when, waits on what
awb build status --ended     # each run that ended since the last call, once
awb build show B-012         # the agent's last message, in your own terminal only
awb build answer B-012 "take 10 MB"
awb build retry B-012        # a new run from main
awb build widen B-012 awb/tcp/keys.py
awb build widen B-012 --tests-replaced SB1-20   # into the run's task.md, which the checks read
awb build stop               # the runner ends after the current task
awb build stop C-321         # a queued check never starts; a running one ends, its suite unit stopped
awb build wait C-321 B-012   # blocks until the first ends (--all: each), one line each; exit 0 when each passed
awb build nudge              # a mail with your next commands for a task asked, red or held for 15 minutes
awb build quick              # before the full run: import rules, gate selftest, the test files of the changed modules
awb build check pr-12        # a branch from outside: the gate and the suite as awb-build, nothing merged
awb build check pr-12 --allow-protected   # the same, a change of seal/, plugin/hooks/, rules/ ... let through
awb pr take 12               # gh says it is yours; fetch, rebase onto main, the check, merge --no-ff, push, close
```

The push of a green `main` to GitHub follows `build_push` in the host file (`/etc/awb/paths.conf`), which has three values:
`yes` (the default: the runner pushes `main` whole), `no` (it never pushes; done is main.git) and `deployed` (it pushes
`main` only up to the commit `/opt/tcp-awb/DEPLOYED` names as live). In `deployed` mode a `main` that is ahead of the
deployed commit is pushed up to the deployed one and the rest waits for the next retry (every 10 minutes);
`awb build status` says `push: waits for the deploy of N commits`, and `awb deploy --task B-012` still takes the task.
A deployed commit that is no ancestor of main.git pushes nothing. `seal/setup.sh --update` keeps the value.

`model` (opus, sonnet, fable; opus), `effort` (low, medium, high; medium), `budget` (USD, 1 to 100; 10) and `clock`
(minutes, 10 to 480; 90) may be left out. `paths` are the files the task may change, `tests_replaced` the test ids
it may drop or replace, `check` test node ids under `tests/` with `-k` or `-m`. A block with a fault is refused in
one line of `awb build list` that names the key, never the value.

`awb build draft FINDING` writes the add file of a finding for you: the row of a findings table (default
`docs/review-2026-10-13.md`, `--from` another file such as `docs/strategy/F2-threat-model-2026-10-13.md`) and the
section of its id give the title, `paths` (the place column, the module's test file, the document that marks it
fixed), `check` and a prompt with the finding, the defaults and the rules of a fix. Above `tests_replaced: none` it
lists as comment lines the rows of `build/tests.json` that run a touched test file: name the ones the fix replaces,
then `awb build add` the file, which skips the comment lines. The draft never goes into the queue by itself.
After it queues a task, `awb build add` prints "tests that import the modules of paths:" and every file under
`tests/` that imports a module of the `paths` line (a plain scan of the `import` and `from` lines, no run), and when
`tests_replaced` is none the line "if the task changes a behaviour these tests assert, name them in
tests_replaced". The queue entry stays as it is.

The runner (`awb build serve`, the unit `awb-buildd.service`) takes the next task whose `depends` are done and whose
`paths` meet no path of a task that is not done, one at a time. The files every task may change (`README.md`,
`CHANGELOG.md`, `COMMANDS.md`, `INTERFACES.md`, `build/tests.json`) reserve nothing: a task that waits on your
answer never blocks the next one for them; `awb build add` warns when a block lists one. It runs as you, never as a model. It starts each
task as `awb-build` in units of a fixed form: a fresh home from the root template with your login, a clone of
`main` from a bundle, the headless session with the block's model, effort, budget and clock, then the hand-over of
the branch as a bundle. Every unit has a private `/tmp`, an IPC namespace and a `/dev/shm` of its own (a tmpfs of
64 MB that goes with the unit) and no `/dev/mqueue`, so nothing a task leaves in shared memory outlives it or reaches
the next task. The hand-over and the rebase units write only the task's clone and out folder, see no
other clone, home or login, have no network and refuse a clone whose git configuration could run a command of the
session (`.git/commondir`, `config.worktree`, `info/attributes`, a key outside a fixed list). The runner judges
the bundle in `main.git`, never the clone. When `main` moved since the session started, the branch is rebased onto
the `main` of now before the checks (after `widen` and the checks of a held run too), so the suite and the
hollow-test check never judge it against an old baseline. `CHANGELOG.md` and `build/tests.json` merge with the drivers of
`awb pr take` (lines of one week and section on both sides, rows by id); the driver is a copy of the runner's own
`awb/mergedriver.py`, never the branch's. A rebase that conflicts in any other file is `red: rebase conflict` and the
check line names the files. The
checks: the unit ended by itself, the client ended well, the first or the last line of the report is `DONE:`, no line is `BLOCKED:` and the report does not end with a question, one to five commits as you on `task/<ID>` and nothing else, no changed blob and no commit above 16 MB (sized with `git cat-file --batch-check` before any read), the gate
(no content check: only a co-author line that names an assistant and a text over 8 MiB are red), the paths, the CHANGELOG and `build/tests.json`, then the whole suite, every test that passed before
still passing and the block's `check` on a `git archive` export, as `awb-build` on loopback only. When the whole
suite has failures, the failed nodes alone run once more in the same suite unit; when they pass the check passes
with a line `flaky: NODE...` in `checks.log` and `result.md`; a node flaky a second time in seven days adds a line
to `build/queue/FLAKY.md` for you. The baseline, the block's `check` and the hollow-test check never rerun. Green moves `main`
in `main.git` (a rebase first when `main` moved), pushes it to GitHub after the exit gate with the register over the whole range (names, secrets, keys, tokens, home
paths and your blocklist; an id is no finding) and ends
`done`. Otherwise the run is `hung`, `red: <reason>`, `asked`, `stopped: <subtype>`, `stale` or `held: <reason>`,
and nothing moves. The session's unit has no loopback (in the host's network namespace it would reach sshd and the
web units), so its prompt tells the agent to run only the module tests that need no network and leaves the whole
suite to the runner. Before the first task of an ISO week the runner opens that week's entry in `CHANGELOG.md` on
`main` of `main.git` as its own commit with your identity (the old week loses `(in progress)`); pull from
`main.git` before you add a line of your own. Each ended run writes `/srv/awb-build/runs/<ID>/<n>/result.md` (fixed fields only), one line on
the board (`state.tsv` in `tcp-shared/build`) and one mail. The board's build line leaves out a task or check
that is done, stopped or passed 48 hours before the newest row of `state.tsv`. Only an API error before the first tool call is retried
by itself, once, ten minutes later. A task that waits on you (`asked`, `red: <reason>` or `held: <reason>`) for
15 minutes with no answer, widen or retry since gets one more mail from `awb build nudge`, which the root owned
`awb-build-nudge.timer` runs as you every 15 minutes: its body is your next commands (`awb build show ID` then `awb
build answer ID "..."`, `awb build retry ID` or `awb build widen ID PATH...`, or the hold's own command), never the
agent's text, once per state, and the board line adds `nudged: B-013 asked 23:30`. `awb build selftest` runs the runner's planted cases in a throw-away folder, which is also its working folder while they run, so
they hold from any folder you start it in, and fails when one of them is missing; `awb build probe` (as you) starts a unit of the runner's own form with the network
of a task: the model endpoint and, with your login, `platform.claude.com` connect, a TCP API host, GitHub and port 22
of `127.0.0.1`, of `127.0.0.53` and of the host's own address do not (a refusal counts only after the model endpoint
connected in the same run). A task unit never reaches a port of the host itself and no nameserver: the runner
resolves the model endpoint (and the token host) when the task starts, allows those addresses alone and binds a
hosts file of loopback and those names over `/etc/hosts` and a `resolv.conf` without a nameserver over
`/etc/resolv.conf`, both written into the run folder. The probe's hosts file also names the TCP API host and
GitHub, so their refusal is the address filter. Without an upstream nameserver in
`/run/systemd/resolve/resolv.conf` the queue is still held as `no resolver for the task unit`.
`sudo seal/verify.sh` runs both; `awb seal check` runs the selftest (the same cases as yours, also as the work user from a folder it cannot
enter) and reads the record of the last probe. Every
task unit names the oneshot `awb-task-failed@.service` in `OnFailure=`: a task unit that fails while no runner
waits on it (the runner stopped or crashed) adds `no runner watched: B-012 failed 14:02 (timeout)` to the board line
until the runner starts again and reports the task itself. The unit has `MemoryMax=2G`, so a read that grows ends the runner and not the vault daemon or the key service. The seal installs it and leaves it to you to enable: start it with
`sudo systemctl start awb-buildd.service` (or `enable --now` for every boot), stop it with `awb build stop`.

A branch from outside, a pull request of a cloud session, is never tested in your shell: its tests and its
`conftest.py` would run as you with the vault readable. Fetch it into your working copy and run `awb build check
REF` there. The runner takes the commit into the quarantine of `main.git` and runs on it what it runs for a task:
the gate (a co-author line that names an assistant, a text over 8 MiB; no content check), the whole suite and the
hollow-test check,
as `awb-build` in the task's suite unit. When `main` moved past the branch's merge base, its changes go onto the
`main` of now first (`git merge-tree` in `main.git`, no working tree, CHANGELOG.md and build/tests.json by the merge
drivers) and the suite runs on that; a conflict in another file is `red: rebase conflict` with the file names. It goes through the runner's lock, so
it never runs beside a task: with no runner it runs at once, else it waits until the runner takes it before its
next task. No model session runs and nothing is merged, pushed or deployed. It prints the state (`passed` or the
first failure, as `red: gate name` or `red: tests lost`) and the check lines, exits 0 only for `passed` and keeps
`runs/C-001/1/result.md`; `awb build list` and the board show the newest three checks. A branch that changes
`seal/`, `plugin/hooks/`, `rules/`, `.claude/`, `CLAUDE.md` or `CLAUDE.local.md` is `red: protected path` before
any code runs, with a line that names the paths: read their diff, then `awb build check REF --allow-protected` lets
them through and the check line names them. Merging stays your step.
A check records the branch name REF in `ref` beside `head`. A new `awb build check` of the same branch replaces a
check of an older head that is still queued: that one ends `stopped: replaced by C-MMM` and never runs. `awb build
stop C-NNN` ends a queued check with `stopped: by_owner`; a running one gets the same state and its suite unit
`awb-task-C-NNN-suite.service` stopped with the runner's own `sudo -n systemctl stop`. `awb build wait ID...` reads
the state files every 5 seconds until the first given check or task ends, or each with `--all`, and prints one line
per ended one: id, state, time and the path of `result.md`. It exits 0 when each printed one passed (a check
`passed`, a task `done`), 1 when not and 2 after `--timeout SECONDS`.
`awb pr take N` does it in one command: it asks gh whose pull request it is and refuses one from a fork or by an
account other than the repository's owner (and refuses without gh), fetches pull request N, rebases it onto your
`main` in a temporary worktree (a conflict in `build/tests.json` merged by id, one in `CHANGELOG.md` by week, any
other one stops it), runs `awb build check` on the result (`--allow-protected` is passed on) and only when that
passed merges it with `--no-ff` (the merge message gets the exit check: names, secrets, paths and the blocklist, no
id), pushes `main` to origin and to `main.git` of the build and closes the pull request with a comment that names the
merge commit. On a failure `main` stays as it was. It never deploys;
`awb pr take N --dry-run` prints the steps.

What the build side does not prove (the accepted limits of `presentations/self-build/THREAT-MODEL.md`, section 6):

- A green suite proves the code against the suite, not against the host. Your look at `result.md` and the diff
  stat and your deploy stay between a task and the running Workbench.
- The clones of one build user are kept apart by the guard hook and the unit's hidden paths, not by a user per
  task. Tasks run one at a time; a second slot would need a second build user.
- The runner has your reach (sudo, the token, the sockets). It is small, has no model, is installed and is
  reviewed like the deploy.
- `result.md` carries commit subjects and a diff stat the agent chose, after `awb check`. A subject line is a short
  channel that stays open; `message.txt` is for your terminal only.
- The address list of a task is resolved when it starts; an endpoint that moves during the task ends it `stopped`.
- `result.md` shows the commit subjects, the test ids and the last lines of the suite log as written: the build side
  runs no content check inside (DMZ 3). A name never reaches GitHub, because the push of `main` has the exit check with
  the register.

The agent's free text stays out of your sessions: add one line to the `deny` list of `permissions` in your own
`~/.claude/settings.json`, `"Read(//srv/awb-build/runs/**/message.txt)"`, and read the message with
`awb build show ID`.
