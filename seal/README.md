# The seal

The seal keeps the register of names away from every working session. It uses two users on the same host.

- The owner is the user who runs `sudo`. He keeps the vault (`<owner-home>/tcp-vault`) and runs the intake. His
  home gets mode 750, so the work user cannot enter it.
- The work user `awb` runs every working session. It has no sudo and is in none of the groups ubuntu, docker, adm,
  sudo, lxd or the primary group of the owner. Its home holds `tcp-shared/`, `tcp-kb/` and the projects
  `tcp-<code>/`.
- The owner is in the group `awb`, so that the intake can write the outbox (`tcp-shared/outbox`, mode 3770).
- The vault daemon (`awb-vaultd.service`) runs as the owner with the group `awb`. Working sessions ask it for the
  name check through `/run/awb/check.sock`. They never see a form or a code of the register.
- The code runs from `/opt/tcp-awb` (root owned, read-only for the work user, its own virtual environment): one
  folder per commit in `/opt/tcp-awb/releases/`, and `/opt/tcp-awb/src` is a symlink to the live one.
  `/usr/local/bin/awb` runs it. `/etc/awb/paths.conf` tells every `awb` command where things are.
- The web side (the gateway, the console data, creation, materials and the project chat) has its own templates
  and switch steps in `web/README.md`; `setup.sh` does not install them.
- A code change goes live with one command, `sudo awb deploy` (section "The deploy" below).
- The build user `awb-build` (section "The build side") will run the unattended build tasks of the self-build
  (`presentations/self-build`). It is a system user in its own group alone, with no password and no sudo, outside the
  group `awb`: it reaches neither the vault, nor the sockets, nor the projects, nor the host file. Its folder
  `/srv/awb-build` is a 20 GB loop image, so a runaway task fills the image and never the filesystem of the vault.

## Files

- `setup.sh`: sets all of the above up. Idempotent, prints every command before it runs it.
- `verify.sh`: proves the seal as root, one line per check, PASS or FAIL, exit 1 on any FAIL.
- `awb-vaultd.service`, `awb-keyd.service`, `awb-ask.service`: the units of the vault daemon, the key service
  and the Ask page. `setup.sh` installs every `*.service` of this folder and
  fills in the owner in the `User=` lines. These templates are the unit list of `awb deploy`, with the templates of
  `web/`.
- `awb-egress.service`, `awb-egress.timer`: the egress filter of the work user (section "The egress filter"). Root
  runs `awb egress load` at boot, before the logins and the units of the work user, and every 5 minutes.
- `egress/ALLOW.md`, `egress/work-egress.nft`, `egress/ask-egress.nft`: the allow list with the reason per destination and how it was
  measured, and the nftables templates of the filter (the work user and the Ask uid, two tables).
- `needrestart-awb.conf`: goes to `/etc/needrestart/conf.d/awb.conf`, so that a package upgrade never restarts
  the vault daemon or the key service: a restart locks them and the console loses projects and chat.
- The package holds (T9 step 4, RT-26): `apt-mark hold cloudflared terraform`, so the routine upgrade leaves the
  tunnel and Terraform alone; an upgrade is a deliberate step (`apt-mark unhold`, upgrade, `apt-mark hold`).
- `work-claude/settings.json`: the client settings of the work user: the six Workbench hooks (`/usr/local/bin/awb hook
  NAME`) and deny rules for every connector tool (`mcp__*`) and for every tool of the client that reads or sends a file,
  runs a command or starts work elsewhere and that the tool guard does not see (`DENIED_TOOLS` of `awb/toolguard.py`:
  PowerShell, LSP, the artifact and worktree tools, RemoteTrigger, SendUserFile, the MCP resource tools).
  Connectors of the account would hand raw customer text to a session. Workflow is not denied: the tool guard takes
  it and passes only the Workbench's own `workflows/review.js` and `workflows/request-check.js` by their scriptPath.
- `work-claude/CLAUDE.md`: the instructions of the work user, at most 60 lines.
- `work-claude/managed-settings.json`: the six hooks once more, installed as a managed drop-in of the client.
- `awb-buildd.service`: the runner of the build queue, as the owner with the group `awb`, `NoNewPrivileges=no` (it
  calls `sudo -n systemd-run`) and `UMask=0022`. `setup.sh` installs it and does not enable it: the steering
  session starts it by hand for the first trial task and enables it on his word. A deploy never starts it and
  defers its restart while a task runs.
- `awb-task-failed@.service` (written by `setup.sh` with the units, not a file here: a template unit has no state
  a deploy could read): the oneshot every task unit names in `OnFailure=`. It runs `awb build failed <unit>` as the
  owner with the group `awb`; while a runner holds its lock it writes nothing (the runner reports the unit), else
  one `failed` row in `tcp-shared/build/state.tsv`, which the board line shows until the runner writes the file
  again.
- `build-claude/settings.json`: the client settings of a build task (section "The build side").
- `../hooks-build/`: the commit hooks of a build clone, installed with every release as
  `/opt/tcp-awb/src/hooks-build`.

The hook `pre-tool` keeps a session inside its project. Read, Edit, MultiEdit, NotebookEdit, Write, Glob, Grep, Bash and
Monitor are refused when their target (the file argument, the path and the pattern of a search, for a shell command
every path of the command line after the shell's quote removal, a relative one taken against the folder each `cd` leaves
it in) lies in another `tcp-*` project folder, in the work user's `~/.claude`, `~/.ssh` or `~/.config`, in
`tcp-shared/sessions` (the receipt marks and `guard.log`), or is a folder that holds one of them. Links are followed,
each before a `..` after it climbs. A shell path the guard cannot read is refused too: a quote or an escape inside a
word that carries a path, a variable or a substitution in a path other than `$HOME` and `$PWD` at its start, `~-`, a
`cd` it cannot follow. The own project, the rest of `tcp-shared` (outbox, datasets), the knowledge base, `/tmp` and the
installed code pass. The refusal is one line with the class of the place (another project, the client's folder, a key
folder, the session records, a path the guard cannot read), never the path, and one line in
`tcp-shared/sessions/guard.log` (time, session id, tool, class, project). The stop hook counts them per project and day
in `tcp-shared/english/refused.tsv`. The hook fails closed: when the project folder of the session cannot be determined,
or the input cannot be read, the call is refused. The owner's sessions are not guarded. `awb hook selftest` plants
refused cases and fails when one passes; `awb seal check` runs it.

`awb seal check` runs the checks of `verify.sh` that need no root, for the user who runs it. Run it as the work
user. On a host with the build side it also runs `awb build selftest` and reads `tcp-shared/build/probe.tsv`, the
record of the last `awb build probe` that `verify.sh` ran (a unit as the build user needs root, which the work user
lacks): it passes only when every network check of the build model passed.

## The egress filter

An nftables table, `inet awb_egress`, for the work user's uid alone (D-M16 of `presentations/modes/DESIGN.md`, here
on the sealed host). Loopback (the resolver, the local services) passes. A new connection on tcp 443 to an address
of the allow list passes and gets the conntrack mark `0xa0b001`: the host names of `egress/ALLOW.md`, resolved by root
every 5 minutes, IPv4 and IPv6. An established or related packet passes only with that mark. Every other new
connection is counted in a named counter of its class (`probe`, `metadata`, `dns`, `ntp`, `tcp_api`, `github`, `ssh`,
`mail`, `web`, `other_tcp`, `other_udp`, `icmp`, `other`), marked `0xa0b002` and then:

- in the mode `count`, accepted. The filter ships in this mode and changes nothing for a session.
- in the mode `enforce`, refused (a reset for tcp, an ICMP refusal for the rest).

An established packet without the mark `0xa0b001` goes the same way uncounted: it passes in count and is refused in
enforce. So a connection opened outside the list while the filter counted does not survive the switch (E1 of
`docs/review-2026-10-13.md`) and a listener of the work user never answers an inbound connection in enforce, since
an inbound connection is never marked (E2; its SYN-ACK is the first answer refused). `sudo awb egress enforce` also
deletes the conntrack entries marked `0xa0b002` with `conntrack -D`, IPv4 and IPv6, when `conntrack` is installed;
without it the entries stay until they time out and their packets are refused all the same. `verify.sh` reads from
the loaded chain that an established packet passes only with the mark; it does not try a live connection across the
switch, which needs a peer outside loopback and a change of the host's mode.

The mode is the line `egress = count|enforce` of `/etc/awb/paths.conf`, read from that root file alone, count when it
is missing; the setup writes count and keeps the line it finds. `sudo awb egress enforce` switches, and is refused
unless the last 24 hours were counted without a gap and no class counted a connection but `probe` (the planted one
of `verify.sh`) and the classes the owner names with `--accept`. `sudo awb egress count` goes back. Every other uid
passes untouched: the key service and the vault daemon run as the owner, so the test tenants and the buckets stay
reachable through the key service and are not on the list (they are watched as `tcp_api`, so a leased key used from
a session shows). `/var/lib/awb-egress/` keeps the resolved addresses, the counts per class and time and the status
that `awb egress status`, `awb seal check` and `awb deploy status` read; never the address of a connection.

## Order

1. Read what would happen: `seal/setup.sh --dry-run` (no root needed; `SUDO_USER` must name the owner, as sudo
   sets it). Every command is printed and none is run. A line with `# if present` depends on the host.
   `AWB_SETUP_BUILD_HOST=fresh` or `present` makes the build side's probes answer for a host without or with the
   build user and its venv, as the tests do. `AWB_SETUP_CONF=FILE` (and `AWB_VERIFY_CONF=FILE` for
   `seal/verify.sh --dry-run`) takes the kept values from FILE instead of `/etc/awb/paths.conf`; a FILE that is not
   there stands for a fresh host. The tests set both, so a plan never depends on the host file of the host it runs on.
2. Set it up: `sudo seal/setup.sh`. Add `--mirrors DIR...` for read-only bind mounts of public doc mirrors under
   `/srv/tcp-mirrors/` (one fstab line each).
3. Prove it: `sudo seal/verify.sh`. `--dry-run` lists the checks. The egress filter runs in the mode count; after a
   day, read `awb egress status` and decide on `sudo awb egress enforce`.
4. Unlock the vault as the owner: `awb vault unlock`. Then log in as `awb` (the owner's ssh keys work) and run
   `awb seal check`.
5. From then on, after every commit: `cd ~/tcp-awb && sudo awb deploy` (`awb deploy --dry-run` shows the plan
   without root).

## What setup.sh does

1. Creates the group and the user `awb` and takes the user out of the groups above. Creates the system user
   `awb-ask` of the Ask page (no login, no home, the group `awb`).
2. Adds the owner to the group `awb`.
3. Sets the home modes. The vault and the password store get mode 700 and nothing else: every other step that
   would touch them is refused. The whole plan is checked once before the first change, so a refused step stops
   the setup before anything changes.
4. Moves `tcp-shared`, `tcp-kb` and the projects listed in `projects.tsv` from the owner's home to the work home
   and writes the new project paths into `projects.tsv`. Creates `tcp-kb` as a git repository when it is missing.
5. Installs the code: `git archive HEAD` of this repository into `/opt/tcp-awb/releases/<commit>` (through a
   `.partial` name, not writable for others), a virtual environment with the system site packages and one line in
   its `awb.pth`, `/opt/tcp-awb/src`, so that `awb` finds `rules/` and `CLAUDE.md` next to its package. pip does not
   run; the files of an earlier editable install are removed after the `.pth` line is in place. The last step is
   the flip: a new symlink `src.next` to the release and one `renameat2(RENAME_EXCHANGE)` with `src`, so `src`
   resolves to a full tree at every instant. On the first run `src` is still a plain folder; the exchange swaps it
   out and it moves to `releases/pre-deploy`. Nothing ever removes `src`. `/usr/local/bin/awb`
   is a root owned wrapper of three lines that runs `/opt/tcp-awb/venv/bin/python3 -I -m awb`: `-I` ignores the
   invoking user's site folder, `PYTHONPATH` and the working folder, so no package planted there is loaded ahead
   of the installed code. The `AWB_*` variables are not `PYTHON*` variables and still apply.
6. Writes `/etc/awb/paths.conf` with the owner, the shared side, the vault, the projects root, the knowledge base,
   the check socket, the egress mode (`egress = count`, or the line it finds) and the build user and its folder
   (`build_user`, `build_root`). `/etc/awb` is root:awb 751, `paths.conf` and `blocklist.txt` 640 root:awb: the owner,
   the work user and the Ask page read them, the build user and the other service users do not (`awb-owner` still
   reaches its own file there). `awb egress enforce|count` and `awb web host` keep that mode and group.
7. Installs every `*.service` of this folder, enables and starts them (a failed start only warns: the Ask page
   waits for its key file, which stays the owner's step). A unit that runs is left running: `sudo awb deploy`
   restarts what a commit changed. The vault locks on a stop, a crash or a reboot. `awb vault reload` (what `awb
   deploy` uses after a code update) hands the passphrase to the new process over a private socket pair and keeps
   it unlocked; the key service keeps its keys the same way. A restart (a changed unit file, a daemon from before
   the hand-over, `--restart`) locks the vault and the deploy asks the passphrase once.
   While the vault is locked every prompt of a work session is refused with the time of
   the lock (`awb hook prompt` exits 2) and a session starts without its project files. Writes `/etc/needrestart/conf.d/awb.conf`: needrestart, which unattended upgrades run,
   leaves the vault daemon and the key service running.
7a. Loads the egress filter (`awb egress load`, section "The egress filter") in the mode of the host file and
   enables `awb-egress.timer`.
8. Copies the owner's `~/.ssh/authorized_keys` to `/etc/ssh/authorized_keys/awb` (root, 644) and writes the sshd
   drop-in `/etc/ssh/sshd_config.d/awb-work-user.conf`, which points the work user alone at that file
   (`AuthorizedKeysFile /etc/ssh/authorized_keys/%u` in a match block for the user `awb`). `sshd -t` checks the
   configuration before the reload; when it refuses, the drop-in is removed again and the setup stops. The old copy
   `<work-home>/.ssh/authorized_keys` goes. The owner's own ssh stays as it is. A hijacked session of the work user
   can no longer add a key of its own (F2 G3).
8a. Turns the work user's crontab into root owned timers (F2 G3): `seal/work-jobs/` holds two oneshot services that
   run as the work user with the usual hardening (no new privileges, a read-only system, write access to the work
   home alone) and their timers, `awb-work-update.timer` at 01:35 (`claude update` of the work user's client into
   `<work-home>/.local/state/claude-update.log`) and `awb-kb-commit.timer` at 01:40 (the commit of the knowledge
   base, no network). The kb commit runs `/usr/local/libexec/awb/nightly-kb-commit` (root) when the repository
   carries `seal/work-jobs/nightly-kb-commit`; it does not today, so the setup says so and the timer runs
   `<work-home>/.local/bin/nightly-kb-commit` where the crontab ran it. Then `/etc/cron.deny` names `awb` (other
   lines stay) and the crontab of the work user goes; a line in it other than the two jobs is named first. Beside
   them `awb-kb-recheck.timer` runs the weekly read tier of the fact-check, Sunday 02:30 UTC up to 10 minutes later
   and no catch-up at boot: `awb kb recheck-run --tier read` as the work user, unix sockets only,
   after `awb-kb-commit.service`, never the write tier (fact-check F9). The unit names no tenant: the run takes
   `recheck_tenant` of `/etc/awb/paths.conf`, which the owner sets and `setup.sh` keeps when it rewrites the file.
   `verify.sh` proves the root owned key file, the file `sshd -T -C user=awb` names, no key file in the work home,
   `cron.deny`, a refused `crontab -l` as the work user, no crontab, the three timers enabled with their services as
   the work user and the recheck service running the read tier alone; a missing or empty `recheck_tenant` is one
   WARN line, not a failure.
9. Installs `work-claude/settings.json` and `work-claude/CLAUDE.md` into the `.claude` folder of the work home,
   owned by root, readable for the work user and immutable (`chattr +i`): the folder is the work user's own
   (the client keeps its login and history there), so without the flag it could rename them and put its own
   files in their place. A later run takes the flag off, installs the new copy and sets it again.
10. Installs `work-claude/managed-settings.json` as `/etc/claude-code/managed-settings.d/awb-workbench.json`: the
   six hooks as managed settings of the client. No user or project file can switch managed hooks off (a
   project file with `disableAllHooks` can switch off the hooks of the user settings). Managed settings apply to
   every user of the host, so `awb hook` exits 0 at once for every user but `work_user` of `/etc/awb/paths.conf`:
   the owner's own sessions are not checked. The deny rules (the connectors and the tools the guard does not
   see) are not managed settings, because they would take those tools of the owner's own sessions too; they
   stay in the immutable settings of the work user, and no project file can allow what a user file denies.
11. Sets up the build side (section "The build side"), in the first seal and in every update.

Home folders come from `getent passwd`. No home path is written into any file of this folder.

## The build side

The build user `awb-build` and what it needs, set up by `setup.sh` (and `--update`), proven by `verify.sh`:

- The user: a system user, its group alone, no password, the shell `/bin/bash` (so that `claude login` works once),
  its home `/srv/awb-build/home`. It is in no group but its own, so the check socket, the key service's call socket,
  the host file and the blocklist are closed to it.
- `/srv/awb-build`: an ext4 image `/var/lib/awb-build.img` of 20 GB, allocated at once, formatted once and mounted
  by an fstab line with `nofail,nodev,nosuid`. The image is made only when the filesystem keeps 10 GB free after it;
  an existing image is never formatted again; when the mount does not hold, no folder is made on the root
  filesystem in its place.
- On it: `main.git` (the owner's, bare, 755; `main` moves only here; filled once from the repository the seal or
  the deploy ran from), `wt/` (the build user's clones, 755), `runs/` (the runner's records, owner 700), `home/`
  (the build user's own, 700), `venv/` (the suite's venv, root owned: the system packages and the test extras) and
  `home-template/` (root owned and immutable: `.claude/settings.json` from `build-claude/`, `.claude/CLAUDE.md` of
  the repository and `.gitconfig` with the owner's name and mail, no `safe.directory`). A template in place is
  never written into: the new one goes into `.home-template.swap` and one exchange swaps it in, so a task unit
  keeps the files it bound; the folder of before stays there without its flags until the next swap. While a task
  unit runs, `sudo awb deploy` passes `--no-build` and the build side waits in `/opt/tcp-awb/BUILD-PENDING` for
  the next deploy or the runner's next start (`--pending-build`, the `ExecStartPre` of its unit).
- `/opt/claude-build`: a root owned copy of the client in the version `setup.sh` pins, taken from the owner's native
  install only when its sha256 is the pinned one; `bin/claude` runs it with the updater and the auto memory off.
- The build settings deny `mcp__*`, `WebFetch`, `WebSearch` and every tool the work user's settings deny, switch the
  auto memory and the commit attribution off and run three hooks: `pre-tool`, `pre-write`, `post-write`. For the
  build user `pre-tool` keeps every call inside its own clone under `wt/`: the clone, the venv (read only), `/tmp`
  and the device paths pass, everything else is refused with its class, and a session in no clone is refused
  outright. `post-write` checks secrets and the bidi override; the names are the runner's, checked with the register
  on `main.git`. The other three hooks of the managed settings exit 0 for the build user.
- `hooks-build/pre-commit` and `commit-msg` run the installed gate with `--no-register` by
  `/opt/tcp-awb/venv/bin/python3`, never code or a `.venv` of the clone.

`verify.sh` proves it as root: the owners and modes above, the image size and mount, the pin, the immutable
template, then N1 to N5 of `presentations/self-build/THREAT-MODEL.md` as the build user (the vault, the host file,
the blocklist, four sockets, `sudo -n true`, two homes and a push into `main.git` each fail) and, in a transient unit
with the properties of a task (`ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateIPC`, a
`/dev/shm` of its own of 64 MB, `NoNewPrivileges`, `/etc/awb` and `/dev/mqueue` inaccessible, `IPAddressDeny=any`
with the resolver and the model endpoint allowed), the model endpoint connects while a TCP API host, GitHub, a write
under `/opt` and `runs/`, a listing of `runs/` and of the work home fail, and the unit sees an empty `/dev/shm` of at
most 64 MB and no `/dev/mqueue`. A probe of that unit counts only after the unit was shown to run as the build user.

- The model credential of a build task (D-S7, changed 2026-10-11): `build_model` in the host file, `login` unless he
  wrote `key`. With `login` the build user runs on his subscription login, made once by him as the owner with
  `sudo -u awb-build -H /opt/claude-build/bin/claude login`; the client keeps it in `/srv/awb-build/home/.claude/`.
  The prepare unit copies it into the task home (600) and the clean unit removes it with the home, after a token the
  task refreshed went back (a refresh may rotate the refresh token). The runner never reads it, the session's unit
  and the suite's do not see the build user's own `.claude`, and the session may reach the token host
  (`platform.claude.com`) beside the model endpoint. Without a login a task ends at once as `held: login missing`
  and holds the queue until he logs in and runs `awb build retry ID`. `setup.sh` copies no key then, removes a copy
  left from the key mode and installs the build settings without `apiKeyHelper`.
- With `key` (the fallback): `setup.sh` copies the source that `build_model_key_source` names
  (`/etc/awb/anthropic.key` unless he changed it) to `/etc/awb/build-model.key`, the owner's alone (400) in the root
  folder `/etc/awb`, again whenever the source changes, and never prints it; the template's settings get
  `apiKeyHelper`. The runner hands the key to each task's home and removes it with the home. It is the owner's file,
  not root's with the owner's group: the runner's unit carries the group `awb`, not his own group, and the host has
  no ACL tools.
- `tcp-shared/build` (the work user's, group `awb`, 3770): the runner writes `state.tsv` there for the board.
- The host file gets `build_model` (login unless he wrote key), `build_model_key`, `build_model_key_source` and
  `build_push` (yes unless he wrote no or deployed: the runner pushes a green `main` to GitHub, D-S2; `deployed` pushes only up to the commit `/opt/tcp-awb/DEPLOYED` names as live; the update keeps the value).

`verify.sh` also names the build model in one line (with `login`: whether a login is there, by its presence only;
with `key`: the copy of the key, the owner's, 400, the same bytes as its source), checks the board's folder and
runs `awb build selftest` as the owner: the runner's planted cases, each refused or classified.

## The deploy

`sudo awb deploy`, run by the owner in the repository after a commit, brings the commit live and restarts only the
units whose code changed. `awb deploy --dry-run` prints the plan and every step without root;
`awb deploy status` prints the journal and the release each daemon runs. What it does, in order:

1. Checks the caller (sudo as the owner of `/etc/awb/paths.conf`, a git work tree in his home, the installed
   command) and takes the lock `/run/awb-deploy.lock`; a second deploy is refused with the pid of the first.
2. Plans as the owner, in a child that dropped root for good: the tree must be clean, the target is HEAD (or
   `--to COMMIT`, or `--rollback`, or with `--task ID` the green commit of a build task after `main` of
   `<build_root>/main.git` was fetched and his `main` fast-forwarded to it), the deployed commit must be known to
   the repository and the target must descend from it unless `--to` or `--rollback` names it (both refusals name
   `git pull --rebase <build_root>/main.git main`), the changed files since the deployed commit map to units through the imports of
   each unit's entry module (as systemd shows it). Root validates the plan before it uses it.
3. Writes the journal `/opt/tcp-awb/DEPLOYED` as `running` (root, 644: commits, unit names, states).
4. Extracts `git archive <target>` (written by the owner child) into `releases/.<commit>.partial`, checks every
   entry (files, folders and links only, no setuid bit), drops the group and other write bits and renames it.
5. Runs `releases/<commit>/seal/setup.sh --update` from the release, never from the working tree. The update mode
   names its steps one by one: the code (the `.pth` line, the wrapper, the flip), the host file, the units, the
   needrestart rule, the package holds, the client files, the managed drop-in and the build side. It never touches the
   work user, the homes, the moves, the ssh keys or the nightly jobs of the work user, so the shared tree is not chowned again. `--units-only` installs the unit templates and reloads
   systemd, nothing else.
6. Restarts the vault daemon, then the key service, then the Ask page, then the web units through
   `releases/<commit>/seal/web/install.sh --only UNIT...` (when the web side was switched). Each must be active
   and keep its main process for 3 seconds within 20; a socket activated service answers one request on its
   socket. A unit that does not come back stays pending in the journal and the run exits 1; the next deploy
   restarts it without a new commit.
7. Asks the vault passphrase once after a restart of the vault daemon and loads the keys after a restart of the
   key service (as the owner). Runs `awb projects sync` as the work user when `rules/`, `work-claude/` or
   `awb/rulesync.py` changed. Prints one status line per daemon: unlocked with its release, `locked since <time>`
   or `down since <time>`, and names a split between a daemon and the code.
8. Marks the journal `done`, appends one line to `/opt/tcp-awb/deploy.log` and keeps the current release, the
   previous one and every release a running unit still uses; older ones are removed.

A run that is interrupted (Ctrl-C, a lost terminal) leaves the journal `running`: the next deploy treats every
unit of that run as pending. `--only UNIT...` restarts the named units even when nothing changed, `--all` every
installed unit, `--restart` restarts the two daemons where a reload would do. The reload (`systemctl reload`, the unit's
`ExecReload`) is chosen when the daemon runs, its unit has the reload, the running daemon names `reload` among its
ops, the target has the hand-over and the unit file did not change; a failed reload with an unchanged main pid puts
the previous code and units back and stops, it never restarts. The first deploy that carries the hand-over restarts
both daemons with one last passphrase prompt. A deploy is refused while an intake holds the vault.

The build runner (`awb build serve`) is the one unit a deploy never starts: when it is not running it takes the new
code at its next start. While an `awb-task-*` unit is active, or when systemd cannot list them, its restart is
deferred: root writes `/srv/awb-build/runs/restart-wanted` (the target, owner 600) and the runner exits between two
tasks with a non-zero code, which `Restart=on-failure` turns into a start with the new code.

## Needs

bash, coreutils, util-linux (`runuser`, `mount`, `mountpoint`, `fallocate`, `losetup`), e2fsprogs (`chattr`,
`lsattr`, `mkfs.ext4`), diffutils
(`cmp`), `useradd` and `gpasswd`, systemd, git, gpg, python3 with venv and setuptools, poppler (`pdftotext`,
`pdfinfo`).

## Limits

- The hooks and the deny rule are a guard rail for honest sessions. The seal itself is the file modes: the work
  user cannot read the vault whatever a session does. A session that starts a client of its own with another
  configuration folder leaves the work user's settings behind (the managed hooks still run).
- The commit gate of a project is a session-owned control: `--no-verify` or another `core.hooksPath` skip it.
  It holds nothing against a hostile session; push stays off, so a committed name stays on the host.
- The deny rule `mcp__*` bounds what a session takes in through connectors, not what it sends out. What it sends out
  is bounded by the egress filter, and only in the mode enforce: until then every connection passes and is counted.
  Even then the allowed hosts stay ways out (an upload to the model API under another account's key, the telemetry
  intake) and so does DNS through the host's resolver; `egress/ALLOW.md` lists the limits. A session holds sanitised
  text only.
- `chattr +i` needs a file system that keeps the flag (ext4, xfs, btrfs). `verify.sh` reports a FAIL when the
  flag is missing.
- A project moved by `setup.sh` gets a new path and so a new memory key of the client. Notes the client kept
  under the old key stay with the owner.
- A mirror folder must be readable for others (mode 755 folders, 644 files), else the work user sees the mount
  but cannot read it.
