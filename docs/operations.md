# Operations

Running the sealed host: the recovery pack, the deploy and the seal. Back to the [README](../README.md).

## Local recovery pack

Run `awb exit pack [--to DIR] [--no-transcripts] [--keep N]` in the owner's terminal to save the work that lives
only in the local folders of the client or outside git (memory, transcripts, the build diary) to a dated folder. The default destination is `~/awb-exit/YYYY-MM-DD/`
using the UTC date. It holds `memory.tgz`, `transcripts.tgz`, `local-only.tgz`, `transcripts-work.tgz` when a work
user is configured and `MANIFEST.txt` with counts, archive sizes, SHA-256 digests and the repository commit.
The owner archive is named `transcripts.tgz`; it takes the place of `transcripts-steering.tgz` in the first manual
pack. The manifest counts regular files and all tar entries separately, so `tar -tzf` matches its entry count.

Memory and recursive `.jsonl` sessions come from this repository's project in `~/.claude/projects/`. Local files
are `CLAUDE.local.md`, `build/PROGRESS.md`, `build/COVERAGE.md`, `build/DECISIONS.md`, `build/OPEN.md`, `build/queue`
and `presentations`. Missing local paths are counted; missing memory is noted. Work transcripts come from the
work user's whole `.claude/projects` folder through `sudo -n tar`. The owner creates the output file, so it stays
owner-owned. If sudo refuses, the other three archives and the manifest are still written and the command exits 1.
The manifest says `work transcripts: not packed (sudo)` and ends with the sudo retry line. Run the pack command
again after a manual retry to refresh the manifest's digests and counts.

Folders have mode 0700 and files 0600. Output contains archive labels, counts and sizes only. A model session,
the work user and the build user are refused. Nothing is uploaded and no model is called. `--no-transcripts`
omits both transcript archives without calling sudo. A second run replaces today's folder. `--keep N` keeps the
newest N dated folders (default 6, minimum 1); other names, files and symbolic links are left alone.

The optional user timer runs as the owner on Sunday at 03:00 UTC and catches missed runs with `Persistent=true`.
The task does not install it. From `~/tcp-awb`, run these two lines in the owner's terminal:

```bash
install -Dm644 tools/awb-exit-pack.service ~/.config/systemd/user/awb-exit-pack.service && install -Dm644 tools/awb-exit-pack.timer ~/.config/systemd/user/awb-exit-pack.timer
systemctl --user daemon-reload && systemctl --user enable --now awb-exit-pack.timer
```

The service expects the checkout and its venv at `~/tcp-awb`; adjust its two paths if installed elsewhere. Its
environment has no model-session marker, so the normal session refusal does not block it. Noninteractive sudo
must permit the tar command for work transcripts. A user timer needs the owner's user manager to be running.

## Deploy

A change goes live with one command after its commit:

```bash
awb deploy --dry-run    # the plan, nothing runs
sudo awb deploy
```

Before anything is installed the plan runs the commit gate over every commit between the live release and the
target and refuses on a finding, naming the commit and the class only; the dry run shows the verdict as `# gate: ...`.
The deploy runs no content check (DMZ 3): a name, a secret, a path or an id passes, and no register is needed. A
message that names an assistant as co-author and a text file over 8 MiB still refuse. The names and secrets are
checked where the commits leave, at the push.
A running `awb winlab get` or `put` holds the deploy back: a deploy replaces the installed tree under it (one killed a
7.86 GB get on 2026-11-25). Both write a start and an end to the events file (see "Winlab") and `sudo awb deploy`
refuses in one line, exit 2 and nothing changed, "a winlab transfer runs for tcp-xxxx since HH:MM; deploy after it ends
or with --force" while a transfer of the last 6 hours has no end and its process lives. `--force` deploys anyway and
cuts the transfer off (the same get again resumes it). A start whose process is gone, a transfer that was killed, is
stale: the deploy says so and goes on. The dry run and a host without a shared side are not held.
The deploy installs the commit as a release of its own and restarts only what the change touched, plus every running
service that still runs an older release (started before the last deploy switched the release, the console gateway
for one): the plan and the dry run show it as `runs an older release`. The running services of the web side (those
the restart lines of `seal/web/install.sh` name) restart in every deploy, `runs the release before this one`, so
needrestart is clean right after it. The name check
and the key service move to the new code without a lock, so no session stops and no key is loaded again. Changed
rules reach every project and every running session, and every daemon that loads a rule file when it starts (the name
check in the vault daemon reads `rules/allow-domains.txt`): the vault daemon reloads and stays unlocked, the others restart. The run ends with the status of each service. A daemon
the deploy left on an older release carries the next command only when a module it imports changed since,
else the note "its code unchanged since".
After the services the deploy publishes the console page of the release (`awb/tcp/web/portal.html`) when its bytes
differ from the live page: the checks of `awb web publish` run as you, the live page is kept as
`index.html.before-deploy-<commit>` and one line says `page published (backup NAME)`, `page unchanged` or `page
refused (REASON, the live page stays)`. A refused page never fails the deploy. A host without the web side skips the
step with one line; `--dry-run` says what the step would do. A change of the page restarts no service.
`awb projects note CODE FILE --after COMMIT [--title TEXT]` stores a held development note. COMMIT is a full
40-character lowercase commit ID. The project prompt shows the code and note handle once per session only after
a successful deploy contains that commit, including as an ancestor. The release journal supplies the ancestry;
a missing, incomplete or failed journal keeps the note held. `awb owner status --dev` lists held notes with their
commits. Deploy ends with one line per newly released note, naming its project code and handle. Without `--after`,
`--title` is required and the note reaches the next prompt immediately.

In development, `POST /api/owner/items/take-default` on the owner host takes `{"code":"tcp-q7m4","item":0}`.
It uses the owner sign-in, exact origin and CSRF checks of publish. `item` is a zero-based index into that
project's OPEN.md owner items in `awb owner status --all CODE` order. The gateway sends the action to
`/run/awb-project-create.sock`. The create service's `projects` role runs as `awb`, checks the owner level
header and reads and checks the item. It then uses the `awb projects note` writer for the fixed text `the owner takes the default of item: ` followed
by its first 110 characters as owner status prints it. Extra request fields are ignored. One ledger receipt
prevents duplicate notes across concurrent clicks and restarts. Invalid indexes and inactive projects are
refused. The next session prompt shows the note; the session acts on the default and closes the item under UX6.

`awb deploy status` shows what runs where, `sudo awb deploy --rollback` goes back to the release before.
`sudo awb deploy --task B-012` brings a green build task live: it pulls `main` of the build side into your copy
first. A deploy from a copy that is behind the live release is refused, so it cannot take back a build task's work.
While a build task runs, the deploy leaves the build side (the home template the task reads) for later: the next
deploy or the build runner's next start applies it, `awb deploy status` names it and `sudo awb deploy --wait-build`
waits up to 60 minutes for the task instead. The build runner restarts through `systemctl` also when the web side is
live. An idle runner exits by itself once the live release is newer than its own. `awb deploy status` names the
runner's release and fails when an idle runner still runs an older one; `seal/verify.sh` checks the same.

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
It also loads the egress filter of the work user: an nftables table that counts every new connection of `awb` to
a host outside `seal/egress/ALLOW.md` (the model API, the client's login, update and telemetry hosts and the TCP
price API), by class. It ships counting and refuses nothing. After a day of counts with nothing unexpected,
`sudo awb egress enforce` makes it refuse; `awb egress status` and `awb deploy status` show the mode and the counts,
`sudo awb egress count` goes back. A second table, `inet awb_egress_ask` (`seal/egress/ask-egress.nft`), holds a chain for the Ask uid (`ask_user` of the host file, `awb-ask` without the line) with a list of its own in `ALLOW.md` (the model endpoint and the price host; loopback and so the host's resolver pass): every other new connection of that uid is counted in `ask_dns`, `ask_web` or `ask_other` and passes in the mode count or is refused in the mode enforce. Its mode is `ask_mode` of the host file (count by default); `sudo awb egress --ask-mode enforce` sets it and `awb egress status` and `awb egress count` print its counts as a block of their own. The work user's table does not change with it, and the Ask table is loaded when the host file carries an `ask_mode` line (the seal writes it). An established connection passes only when the list allowed it: in enforce a connection opened outside the list while the filter counted is refused at its next packet. So is every answer of a listener of `awb` to a connection from outside. The switch also deletes those connections from conntrack when the `conntrack` tool is installed; without it they are refused all the same. The test tenants stay reachable through the key service, which runs as you. When the table is missing within the same boot (a restart of nftables removes it) the next load puts it back, restarts the count window and the status says so.

A project that needs a host off the list asks with `awb dev ask`. The desk or development grants it as the owner
user, without sudo and never from a project folder: `awb egress allow HOST --project CODE --until DATE --why TEXT`
(one host name, a last day at most 30 days ahead, a reason with codes), `awb egress allow --list` and `awb egress
revoke HOST --project CODE`. The grant goes into `~/.config/awb/egress-grants.tsv` (0600), a line into
`~/.config/awb/egress-grants.log` and an entry into the ledger. The next `awb egress load` (the timer within 5
minutes, at once with `sudo awb egress load`) adds the host to the allowed set of the work user and drops it after
its last day or a revoke. The filter knows uids, not projects: a granted host is open to every session of the work
user, and the project code says who asked. A host of `seal/egress/ALLOW.md` takes no grant, and a lasting addition
stays a development commit of that file.
Things you want no commit to carry (names of other workspaces, a naming scheme) go into a local blocklist, one regular expression per
line: `/etc/awb/blocklist.txt` or `~/.config/awb/blocklist.txt`. It never enters the repository.

A fourth user, `awb-build`, is there for the self-build (`presentations/self-build`): unattended build tasks of the
Workbench itself and the checks of a branch from outside run as this user, never as you or as `awb`. It has no password, no sudo and no group but its
own, so it reaches neither the vault, the sockets, the projects nor `/etc/awb` (root:awb 751, the host file and the
blocklist 640). Its folder `/srv/awb-build` is a 20 GB loop image (`main.git`, the clones, the runner's records, the
suite's venv, a root owned home template), its client a pinned root owned copy in `/opt/claude-build`, and its hook
keeps every tool call inside its own clone (the venv may be read and its interpreter run, nothing else of
`/srv/awb-build` passes). It runs on your subscription login, which you make once as yourself
with `sudo -u awb-build -H /opt/claude-build/bin/claude login`; the runner copies it into each task's home and
removes it with the home, and the tasks spend your shared quota. A token the task's client refreshed goes back to
the build user's home only the token fields (`accessToken`, `refreshToken`, `expiresAt`) go, only when it names the same `subscriptionType`
(and the same account id, where the login there has one) as the login there and expires later by at most 48 hours; a login
of the task that names another or none stays where it is and the run's `result.md` says so in one line (a task
cannot replace your login with its own). `build_model = key` in `/etc/awb/paths.conf` switches
to a copy of the Workbench's key (`/etc/awb/build-model.key`, yours alone, 400) in one line. `build_model = auto`
runs on your login and, after a subscription limit (the 5-hour or the weekly one, also a run that ends with api_error
and 0 tokens, which is how the login shows a limit with no text), holds the task as `held: usage
limit`, runs it again and the next ones on that key until the reset (sonnet, opus only for a block whose title names
a security review) and goes back to the login then; `key_week_cap_usd` (25 unless you write another) caps the key
runs of a week, at the cap the queue waits as `key cap`. Each switch is one mail with the spend of the week, and
`awb build list` and `awb owner status` name the mode (`awb/buildbudget.py`). With `login` a limit holds the task
and the queue until the reset. The runner's unit `awb-buildd.service` is installed and
you enable it when you are ready (`sudo systemctl enable --now awb-buildd.service`); `sudo seal/verify.sh` proves the rest. `seal/README.md`, section "The build side", has the details.

| when | what to run |
|---|---|
| after a reboot | `awb unlock` (as the owner: the vault, then the keys, each asks its passphrase); `awb` alone shows what is locked |
| after a commit | `sudo awb deploy`: the vault and the keys stay unlocked |
| to stop what a session could send out | `awb egress status`, then `sudo awb egress enforce` when the last 24 hours counted nothing unexpected |
| after a library upgrade | `sudo awb deploy --only awb-vaultd.service awb-keyd.service` |
| a new project, close or delete | as `awb`: projects belong to the work user |
| a customer's material | as the owner: `awb import tcp-xxxx` (it asks for the customer's names on the first import; a project without a customer needs no names); the session hears of the copies at its next prompt |
| a mail, a transcript or a table you copied | as the owner: `awb import tcp-xxxx --paste --kind mail` (or `transcript`, `table`) with the text on standard input, in your own terminal; the session hears of the copy at its next prompt |
