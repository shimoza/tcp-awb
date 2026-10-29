# Deploy your own Architect Workbench

This guide is for an architect who wants to run the Workbench on his own Linux host. If that is too much, the TCP Facts dataset at github.com/shimoza/tcp-facts is the lighter offer.

The guide describes what the repository really does, taken from `seal/README.md`, `seal/setup.sh`, `seal/verify.sh`, `awb/deploy.py` and the `--help` of each command. A step marked "not run here" was read from the code and not run on a real host by the author of this page. Names in angle brackets, such as `<owner>` or `<work-home>`, stand for values of your host.

## What you get

Two users share one host. You are the owner. You keep the register of customer names, the keys of the test tenants and the intake. The work user `awb` runs every Claude Code session and cannot read what the owner keeps. A change of the code goes live with one command, `sudo awb deploy`.

## What you need

- A host or VM with Ubuntu 24.04 (Python 3.12). The seal needs systemd, `sudo` and a file system that keeps `chattr +i` (ext4, xfs or btrfs).
- A user with `sudo` who becomes the owner. Do not use root as the owner: `setup.sh` refuses it.
- `git`, `gpg`, `python3` with `venv` and the poppler tools (`pdftotext`, `pdfinfo`, `pdfimages`, `pdfdetach`).
- The tools `setup.sh` calls: `useradd`, `gpasswd`, `runuser`, `mount`, `mountpoint`, `fallocate`, `losetup`, `chattr`, `lsattr`, `mkfs.ext4`, `cmp` and `nft` (the egress filter). A stock Ubuntu server has them.
- Claude Code for the work user, with your Claude subscription login or an API key. The repository does not install the client for you.
- Optional: `pass` with a gpg key, for the keys of a TCP tenant. A TCP tenant is only for live checks.
- Optional: a Cloudflare tunnel and a host name, for the web console.
- Optional: 30 GB of free disk for the build side (a 20 GB image and a 10 GB floor).
- Optional: an API key in `/etc/awb/anthropic.key` for the Ask page, owned by the service user `awb-ask` with mode 400. Only that user reads it.

The `awb` command itself needs no model and no API key. Runtime code is the Python standard library plus the poppler tools.

## The order of steps

Run steps 1 to 6 as the owner.

1. Get the code in the owner's home and test it.

   ```bash
   git clone https://github.com/shimoza/tcp-awb.git ~/tcp-awb
   cd ~/tcp-awb
   python3 -m venv .venv
   .venv/bin/pip install -e '.[test]'
   .venv/bin/pytest
   .venv/bin/awb gate --selftest
   ```

   Keep the clone. `sudo awb deploy` later reads it. It must be a git work tree in the owner's home.

2. Create the shared side and the vault side.

   ```bash
   .venv/bin/awb init
   ```

   It makes `~/tcp-shared` (with `outbox` and `ledger`) and `~/tcp-vault` (with `inbox`, `log`, `originals`, `reports` and an empty `register.tsv`). It is safe to run again. `setup.sh` moves the shared side to the work user and locks the vault folder to mode 700.

3. Read what the setup would do. This needs no root. `SUDO_USER` must name the owner, as sudo sets it. Without sudo, set it yourself, else the script stops with "SUDO_USER is empty".

   ```bash
   SUDO_USER=$USER seal/setup.sh --dry-run
   ```

   Every command is printed and none runs. A line with `# if present` depends on the host. The section "What the setup plans" below quotes it.

4. Set the host up.

   ```bash
   sudo seal/setup.sh
   ```

   Add `--mirrors DIR...` for read-only bind mounts of public documentation mirrors under `/srv/tcp-mirrors/`. The script is idempotent and prints every command before it runs it. It checks the whole plan once before the first change, so a refused step stops it before anything changes.

5. Prove the seal.

   ```bash
   sudo seal/verify.sh
   ```

   Every line must say PASS. The script exits 1 on any FAIL. The lines of the build side fail until its disk, its pinned client and its login are there (see "The build runner" below). If you do not use the build side, those FAIL lines are expected. `sudo seal/verify.sh --dry-run` lists the checks and runs none.

6. Encrypt the register and unlock the vault daemon.

   ```bash
   awb vault encrypt     # asks for a passphrase twice, encrypts the register once, then unlocks the daemon
   awb vault status
   ```

   After a reboot the vault is locked. Run `awb vault unlock` then. While it is locked every prompt of a work session is refused with the time of the lock.

7. Give the work user a Claude Code client. Log in as the work user with the ssh keys of the owner, which `setup.sh` copied to `/etc/ssh/authorized_keys/awb`, a root owned file the work user cannot change (a later key for the work user goes there with sudo, never into its home).

   ```bash
   ssh awb@<host>
   ```

   Without ssh keys of the owner, `sudo -iu awb` opens a shell of the work user from the owner's terminal.

   Install Claude Code and log in there as the client's own instructions say. The repository does not do this step. The settings of the work user are already in place and immutable (see the tables below). Use the native install (`~/.local/bin/claude`): the root owned timer `awb-work-update.timer` runs `claude update` as the work user every night at 01:35. The work user has no crontab (`/etc/cron.deny` names it).

8. Check the seal from the work user's side.

   ```bash
   awb seal check
   ```

   It runs the checks of `verify.sh` that need no root and plants refused cases for the hooks.

9. Wait one day, then look at the egress filter. It ships in the mode `count` and refuses nothing.

   ```bash
   awb egress status
   sudo awb egress enforce     # when the last 24 hours counted nothing unexpected
   ```

   `enforce` is refused unless the last 24 hours were counted without a gap and no class counted a connection but `probe` and the classes you name with `--accept CLASS...`. `sudo awb egress count` goes back.

10. Start work as the work user. Projects belong to the work user.

    ```bash
    awb spawn query --goal "Can a managed database run in two regions?"
    ```

    `awb spawn --help` shows `--customer CODE`, `--tag TAG` and `--from-outbox`. The kinds are `query` and `project`. Customer material enters only through `awb import tcp-xxxx` and `awb intake`, run by the owner in his own terminal. From the owner's terminal, `awb projects open tcp-xxxx` starts a session in a project as the work user. It needs `sudo -n` for the work user.

### A TCP test tenant (optional)

A project that needs live checks uses a test tenant. The keys stay with the owner in `pass`. The key service signs the calls and no session sees a key.

```bash
pass insert awb/admin/test-1/domain     # and user, then password
awb tenant setup test-1 --domain-id ID [--region REGION] [--lab-key | --lab-user] [--alerts]
awb keys unlock                         # after every start of the key service
awb keys status
```

`awb tenant setup` makes the read group, the read user and its key in `pass` and the tenant entry (`awb tenant add`). Then it runs `awb keys unlock` and the first snapshot. A second run changes nothing. By hand the same is `pass insert awb/tenant/test-1/ak` and `sk`, then `awb tenant add test-1 --keys pass:awb/tenant/test-1 --region eu-de`. Run `awb tenant --help` for the other commands. Not run here: there is no TCP tenant in the sandbox.

### The Claude Code plugin (optional)

Do not install the plugin on a sealed host. The seal already installs the six hooks for the work user and the plugin would run them twice. The plugin is for a host without the seal:

```bash
claude plugin marketplace add shimoza/tcp-awb
claude plugin install awb@tcp-awb
```

Install `awb` first (step 1). The hooks of the plugin call `awb hook ...`, so `awb` must be on the `PATH` of the client, for example `~/tcp-awb/.venv/bin`. Not run here: the sandbox has no Claude Code client.

### The web console (optional)

The console is the web side of the Workbench. Read `seal/web/README.md` before you start. The state of the repository today is this:

- The repository holds the gateway, the handlers (`awb/tcp/web/`), the owner page (`awb/tcp/web/owner.html`), the unit templates and the tunnel files. It does not hold the main page of the console. You bring your own page and `awb web publish` checks it and installs it to `/srv/awb-web/index.html`.
- `seal/web/README.md` describes the switch from an older production copy in `/opt/awb-web`. On a new host there is no such copy, so treat this part as unproven.
- `setup.sh` does not install the web units.

The steps, as that README gives them:

```bash
sudo awb web host <owner host name>              # once; writes owner_host into /etc/awb/paths.conf
sudo seal/web/install.sh --domain <site host name> --dry-run
sudo seal/web/install.sh --domain <site host name>
awb keys unlock                                  # the restart of the key service emptied it
sudo awb web user add <login> [--level owner]    # the logins of the console
sudo awb web publish FILE                        # the page of the console, checked, with a backup
awb web status
```

`install.sh` needs `owner_host`, `front_socket` and `cloudflared_user` in `/etc/awb/paths.conf` and stops before any change when one is missing. It makes the system users of the console (`awb-console`, `awb-owner` and the user of the tunnel) once. It keeps every unit it replaces as `<unit>.before-switch`. In the Cloudflare dashboard every ingress rule points at `unix:<front_socket>` and the owner host needs its own DNS name. You install `cloudflared` yourself. The setup holds the packages `cloudflared` and `terraform` with `apt-mark hold`. Not run here: a dry run of `install.sh` stops at "/etc/awb/paths.conf cannot be read: run seal/setup.sh first", because the sandbox has no sealed host.

### The build runner (optional)

`setup.sh` prepares the build side: the user `awb-build`, a 20 GB image mounted at `/srv/awb-build` and a pinned copy of the client in `/opt/claude-build`. The unit `awb-buildd.service` is installed and not enabled. The runner needs more than this repository: the self-build design files under `presentations/` are not in the repository, so the build queue has no written design here. Presentations are not included.

- Disk: the image is made only when the file system keeps 10 GB free after it. With less, `setup.sh` warns and the build side waits. Then `verify.sh` reports FAIL lines for it.
- Client: the pinned client version (2.1.296 in `setup.sh`) must be in the owner's native install, in `~/.local/share/claude/versions/`, with the sha256 that `setup.sh` pins. Else the build client waits.
- Login: the build user runs on your subscription login. Make it once with `sudo -u awb-build -H /opt/claude-build/bin/claude login`. With `build_model = key` in `/etc/awb/paths.conf` it uses a copy of `/etc/awb/anthropic.key`.
- Use: `awb build --help` lists `add`, `list`, `status`, `show`, `answer`, `retry`, `widen`, `stop`, `serve`, `selftest` and `probe`. You start the runner by hand and enable it on your word: `sudo systemctl start awb-buildd.service`.

## What the setup plans

This is an excerpt of the output of `seal/setup.sh --dry-run` in a sandbox with an invented owner. The dots mark lines left out.

```text
# dry run for the owner <owner>: every command is printed, none is run
+ groupadd awb
+ useradd --create-home --gid awb --shell /bin/bash --comment Architect\ Workbench\ sessions awb
+ useradd --system --gid awb --no-create-home -d /nonexistent --shell /usr/sbin/nologin awb-ask
+ usermod -a -G awb <owner>
+ chmod 750 /home/<owner>
+ chmod 700 /home/<owner>/tcp-vault  # if present
+ chmod 700 /home/<owner>/.password-store  # if present
+ chmod 2750 <work-home>/tcp-shared
+ mkdir -p /opt/tcp-awb/releases
+ python3 -m venv --system-site-packages /opt/tcp-awb/venv  # if missing
+ write /usr/local/bin/awb (mode 755, root:root)
+ ln -sfn releases/<HEAD> /opt/tcp-awb/src.next
+ exchange /opt/tcp-awb/src.next /opt/tcp-awb/src  # renameat2 RENAME_EXCHANGE, one call
+ write /etc/awb/paths.conf (mode 640, root:awb)
+ systemctl daemon-reload
+ systemctl enable --now awb-ask.service
+ systemctl enable --now awb-egress.service
+ systemctl enable --now awb-keyd.service
+ systemctl enable --now awb-vaultd.service
+ systemctl enable --now awb-egress.timer
+ /usr/local/bin/awb egress load
+ apt-mark hold cloudflared
+ apt-mark hold terraform
+ chattr +i <work-home>/.claude/settings.json
...
# dry run done, nothing was changed
```

The full run prints about 420 lines. The dry run in the sandbox ended with exit 0.

## What the setup creates on the host

Users and groups:

| name | kind | login | home | groups |
|---|---|---|---|---|
| `awb` | work user | shell `/bin/bash`, no sudo | `<work-home>`, mode 750 | `awb` only. It is in none of ubuntu, docker, adm, sudo, lxd or the group of the owner. |
| `awb-ask` | system user of the Ask page | none | none | `awb` |
| `awb-build` | system user of the build side | shell `/bin/bash`, no password, no sudo | `/srv/awb-build/home`, mode 700 | its own group only |
| the owner | your user | unchanged | his home gets mode 750 | added to `awb` |

Folders and files:

| path | owner and mode | what it is |
|---|---|---|
| `<owner-home>/tcp-vault` | owner, 700 | the vault with the register |
| `<owner-home>/.password-store` | owner, 700 | the keys of the test tenants (if present) |
| `<work-home>/tcp-shared` | `awb:awb`, 2750 | exchange side. The `outbox` and `tenants` folders are 3770. |
| `<work-home>/tcp-shared/build` | `awb:awb`, 3770 | the board line of the build runner |
| `<work-home>/tcp-kb` | `awb:awb`, 750 | the knowledge base, a git repository with the commit gate hooks |
| `<work-home>/tcp-<code>` | `awb:awb` | one folder per project |
| `<work-home>/.claude/settings.json`, `CLAUDE.md`, `skills/drafting/SKILL.md` | root, group awb, 644, immutable | the client settings and instructions of the work user |
| `<work-home>/.gitconfig` | root, group awb, 644, immutable | the git identity of the work user |
| `/etc/ssh/authorized_keys/awb` | root, 644 | a copy of the owner's keys (if present), the only key file sshd reads for the work user |
| `/etc/ssh/sshd_config.d/awb-work-user.conf` | root, 644 | points sshd at that file for the work user alone; the owner's ssh is unchanged |
| `/etc/cron.deny` | root, 644 | names `awb`: the work user has no crontab |
| `/etc/systemd/system/awb-work-update.*`, `awb-kb-commit.*` | root, 644 | the nightly jobs of the work user as timers that run as `awb`: `claude update` at 01:35, the commit of the knowledge base at 01:40 |
| `/opt/tcp-awb/releases/<commit>` | root, no group or other write | one folder per commit from `git archive HEAD` |
| `/opt/tcp-awb/src` | root, symlink | the live release |
| `/opt/tcp-awb/venv` | root | the virtual environment with the system site packages |
| `/opt/tcp-awb/DEPLOYED` and `deploy.log` | root, 644 | the journal and the log of the deploys |
| `/usr/local/bin/awb` | root, 755 | three lines that run `/opt/tcp-awb/venv/bin/python3 -I -m awb` |
| `/etc/awb` | `root:awb`, 751 | the host folder |
| `/etc/awb/paths.conf` | `root:awb`, 640 | the paths of the owner, the work user, the vault, the egress mode and the build side |
| `/etc/awb/blocklist.txt` | `root:awb`, 640 | your local blocklist, if you made one |
| `/etc/claude-code/managed-settings.d/awb-workbench.json` | root, 644 | the six hooks as managed settings |
| `/etc/needrestart/conf.d/awb.conf` | root, 644 | keeps a package upgrade from restarting the vault daemon and the key service |
| `/var/lib/awb-egress/` | written by root | the resolved addresses, the counts per class and the status |
| `/run/awb/check.sock` | the owner, group awb | the name check socket |
| `/srv/awb-build`, `/var/lib/awb-build.img`, `/opt/claude-build` | see the build runner | the build side |

Units in `/etc/systemd/system/`:

| unit | runs as | state after setup |
|---|---|---|
| `awb-vaultd.service` | the owner, group awb | enabled and started. It is locked until you run `awb vault unlock`. |
| `awb-keyd.service` | the owner, group awb | enabled and started. It holds no key until you run `awb keys unlock`. |
| `awb-ask.service` | `awb-ask` | enabled and started. It waits for its key file. |
| `awb-egress.service` and `awb-egress.timer` | root | loaded at boot and every 5 minutes |
| `awb-buildd.service` | the owner, group awb | installed, not enabled |
| `awb-task-failed@.service` | the owner, group awb | installed, a template for failed build tasks |

The setup also adds one line to `/etc/fstab` for the build image (`loop,nofail,nodev,nosuid,noatime`), one `bind,ro` line per mirror of `--mirrors` and runs `apt-mark hold cloudflared terraform`.

Not run here: the sandbox has no systemd, no second real user and no `sudo`. This list comes from the dry run and from the text of `setup.sh` and `seal/README.md`.

## How to check it

`sudo seal/verify.sh` prints one line per check. The main groups are:

- `the work user cannot read the register`, `... list the vault`, `... enter the owner's home`, `... run sudo -n true`
- `the work user is not in the group` ubuntu, docker, adm, sudo and lxd
- `the check socket answers ping`, `the work user can write tcp-shared`, `the owner can write the outbox`
- `the client file settings.json of the work user is the one of the repository` and `... is immutable`. The same checks exist for `CLAUDE.md`, the skill and the git identity
- `the managed client settings carry the Workbench hooks`
- `the egress table of the work user is loaded`, `... runs in the mode of the host file`, `a planted connection of the work user outside the list is counted` and `the timer of the egress filter is active`
- `/etc/awb is root:awb 751, paths.conf and blocklist.txt are 640 root:awb`
- the build side: the user, the image, `main.git`, the venv, the home template, the pinned client, `the build runner refuses its planted cases (awb build selftest)`, the checks that the build user cannot reach the vault, the host file, the blocklist, the sockets or sudo and the probe unit checks
- `the probe unit reaches the model endpoint` and `... cannot reach github.com`

The probe unit checks need network access to `api.anthropic.com` and `platform.claude.com`. As the work user, `awb seal check` runs the checks that need no root. `awb deploy status` shows the journal and the release each daemon runs. `awb vault status` and `awb keys status` show the two daemons. `awb egress status` shows the mode and the counts.

## How to update

```bash
cd ~/tcp-awb
git pull
awb deploy --dry-run     # the plan and every step, no root needed
sudo awb deploy
```

The tree must be clean and the target must descend from the deployed commit. `sudo awb deploy` installs the commit as a release of its own, runs that release's `setup.sh --update` and restarts only the units whose code changed. A changed unit file of `seal/work-jobs` (the nightly jobs of the work user and the weekly fact-check) is installed by the update too, and only its changed timer is restarted. The vault daemon and the key service move to the new code without a lock. A restart of a daemon locks it. The deploy then asks the passphrase once. A second deploy while one runs is refused. Useful options from `awb deploy --help`:

- `--only UNIT...` restarts the named units even when nothing changed.
- `--all` restarts every installed unit.
- `--restart` restarts the two daemons where a reload would do.
- `--to COMMIT` deploys that commit.
- `--mirrors DIR...` is passed to `setup.sh --update`.
- `--task ID` brings the green commit of a build task live.

After a library upgrade run `sudo awb deploy --only awb-vaultd.service awb-keyd.service`. A run that is interrupted leaves the journal `running`. The next deploy treats every unit of that run as pending. `awb deploy status` shows where it stands. Not run here: `awb deploy` stops in the sandbox at "run it with sudo as the owner".

## How to roll back

```bash
sudo awb deploy --rollback
```

This deploys the release that was live before the current one (the `previous` entry of `/opt/tcp-awb/DEPLOYED`). It is refused with "there is no earlier release to roll back to" when there is none. `--rollback`, `--to` and `--task` exclude each other. A deploy keeps the current release, the previous one and every release a running unit still uses. Older releases are removed. A rolled-back release also brings back its own web units when the web side is installed. `sudo awb web publish --rollback` installs the newest backup of the page. `sudo seal/web/install.sh --rollback` is only for a host that switched from an older copy in `/opt/awb-web`, because it restarts the web units on that copy. Not run here.

## How to remove it

The repository has no uninstall script. The steps below are the reverse of `setup.sh`, in the order that is safe. Check each one against your host before you run it. Not run here.

1. Stop and disable the units: `awb-egress.timer`, `awb-egress.service`, `awb-buildd.service`, `awb-ask.service`, `awb-keyd.service` and `awb-vaultd.service` (and `awb-portal.service` on a host sealed before 2026-10-17, if `setup.sh --update` has not removed it yet). Remove their files from `/etc/systemd/system/`, remove `awb-task-failed@.service` and run `systemctl daemon-reload`.
2. Remove the egress table: `nft delete table inet awb_egress`.
3. If you installed the web console, stop and disable the units of `seal/web/` (the `.service`, `.socket` and `.timer` files and the drop-in folder `awb-ask.service.d`) and remove them from `/etc/systemd/system/` with their `.before-switch` and `.off` copies. Remove `cloudflared.service.d/50-awb-fence.conf`, run `nft delete table inet awb_tunnel_fence` and remove `/srv/awb-web`.
4. Take the immutable flag off the client files with `chattr -i` (the files in `<work-home>/.claude`, `<work-home>/.gitconfig` and for the build side `/srv/awb-build/home-template` with its `.claude` folder) before you delete them.
5. Unmount `/srv/awb-build`, remove its line from `/etc/fstab` and delete `/var/lib/awb-build.img`. Unmount the mirrors under `/srv/tcp-mirrors` and remove their lines too.
6. Remove `/opt/tcp-awb`, `/opt/claude-build`, `/usr/local/bin/awb`, `/etc/awb`, `/etc/claude-code/managed-settings.d/awb-workbench.json`, `/etc/needrestart/conf.d/awb.conf` and `/var/lib/awb-egress`.
7. Run `apt-mark unhold cloudflared terraform` if you want the routine upgrade to touch them again.
8. Move `<work-home>/tcp-shared`, `tcp-kb` and the projects out of the work home if you want to keep them. Deleting the user deletes its home.
9. Remove the users: `userdel awb`, `userdel awb-ask`, `userdel awb-build`. For the web console also remove `awb-console`, `awb-owner` and the user of the tunnel (`cloudflared_user`). Remove the groups left. Take the owner out of the group `awb`.
10. Restore the mode of the owner's home (it was set to 750). Keep or delete `~/tcp-vault` and `~/.password-store` as you decide. They are yours. The register in the vault cannot be rebuilt.

## What is optional

- the TCP tenant and the key service entries (`awb tenant setup`, `awb keys unlock`)
- the web console, the Cloudflare tunnel and the owner host
- the build side and its runner
- the egress filter mode `enforce` (the filter itself is installed)
- the Ask page and its API key
- the mirrors of public documentation (`--mirrors`)
- the Claude Code plugin, on a host without the seal

## What the repository does not do

- It does not install Claude Code for the work user or log it in.
- It does not install `cloudflared`, `pass` or `terraform`.
- It does not carry the main page of the web console.
- It does not carry the design files of the self-build under `presentations/`.
- It does not carry the knowledge base of TCP facts. `awb kb` works on an empty one.
- It does not uninstall itself.

## Limits

The hooks and the deny rules are a guard rail for honest sessions. The seal itself is the file modes. The egress filter bounds what a session sends out only in the mode `enforce`. `seal/README.md`, section "Limits", has the rest.
