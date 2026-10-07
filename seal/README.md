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
- The code runs from `/opt/tcp-awb` (root owned, read-only for the work user, its own virtual environment).
- The web side (the gateway, the console data, creation, materials and the project chat) has its own templates
  and switch steps in `web/README.md`; `setup.sh` does not install them.
  `/usr/local/bin/awb` points at it. `/etc/awb/paths.conf` tells every `awb` command where things are.

## Files

- `setup.sh`: sets all of the above up. Idempotent, prints every command before it runs it.
- `verify.sh`: proves the seal as root, one line per check, PASS or FAIL, exit 1 on any FAIL.
- `awb-vaultd.service`: the unit of the vault daemon. `setup.sh` fills in the owner in the `User=` line.
- `needrestart-awb.conf`: goes to `/etc/needrestart/conf.d/awb.conf`, so that a package upgrade never restarts
  the vault daemon or the key service: a restart locks them and the console loses projects and chat.
- `work-claude/settings.json`: the client settings of the work user: the five Workbench hooks
  (`/usr/local/bin/awb hook NAME`) and a deny rule for every connector tool (`mcp__*`). Connectors of the account
  would hand raw customer text to a session.
- `work-claude/CLAUDE.md`: the instructions of the work user, at most 60 lines.
- `work-claude/managed-settings.json`: the five hooks once more, installed as a managed drop-in of the client.

`awb seal check` runs the checks of `verify.sh` that need no root, for the user who runs it. Run it as the work
user.

## Order

1. Read what would happen: `seal/setup.sh --dry-run` (no root needed; `SUDO_USER` must name the owner, as sudo
   sets it). Every command is printed and none is run. A line with `# if present` depends on the host.
2. Set it up: `sudo seal/setup.sh`. Add `--mirrors DIR...` for read-only bind mounts of public doc mirrors under
   `/srv/tcp-mirrors/` (one fstab line each).
3. Prove it: `sudo seal/verify.sh`. `--dry-run` lists the checks.
4. Unlock the vault as the owner: `awb vault unlock`. Then log in as `awb` (the owner's ssh keys work) and run
   `awb seal check`.

## What setup.sh does

1. Creates the group and the user `awb` and takes the user out of the groups above.
2. Adds the owner to the group `awb`.
3. Sets the home modes. The vault and the password store get mode 700 and nothing else: every other step that
   would touch them is refused. The whole plan is checked once before the first change, so a refused step stops
   the setup before anything changes.
4. Moves `tcp-shared`, `tcp-kb` and the projects listed in `projects.tsv` from the owner's home to the work home
   and writes the new project paths into `projects.tsv`. Creates `tcp-kb` as a git repository when it is missing.
5. Installs the code: `git archive HEAD` of this repository into `/opt/tcp-awb/src`, a virtual environment with
   the system site packages, an editable `pip install` of `/opt/tcp-awb/src` (so that `awb` finds `rules/` and
   `CLAUDE.md` next to its package), then everything root owned and not writable for others. `/usr/local/bin/awb`
   is a root owned wrapper of three lines that runs `/opt/tcp-awb/venv/bin/python3 -I -m awb`: `-I` ignores the
   invoking user's site folder, `PYTHONPATH` and the working folder, so no package planted there is loaded ahead
   of the installed code. The `AWB_*` variables are not `PYTHON*` variables and still apply.
6. Writes `/etc/awb/paths.conf` with the owner, the shared side, the vault, the projects root, the knowledge base
   and the check socket.
7. Installs, enables and starts the unit. After a code update restart it yourself: the vault locks and needs
   `awb vault unlock` again. While the vault is locked every prompt of a work session is refused with the time of
   the lock (`awb hook prompt` exits 2) and a session starts without its project files. Writes `/etc/needrestart/conf.d/awb.conf`: needrestart, which unattended upgrades run,
   leaves the vault daemon and the key service running.
8. Copies the owner's `~/.ssh/authorized_keys` to the work user.
9. Installs `work-claude/settings.json` and `work-claude/CLAUDE.md` into the `.claude` folder of the work home,
   owned by root, readable for the work user and immutable (`chattr +i`): the folder is the work user's own
   (the client keeps its login and history there), so without the flag it could rename them and put its own
   files in their place. A later run takes the flag off, installs the new copy and sets it again.
10. Installs `work-claude/managed-settings.json` as `/etc/claude-code/managed-settings.d/awb-workbench.json`: the
   five hooks as managed settings of the client. No user or project file can switch managed hooks off (a
   project file with `disableAllHooks` can switch off the hooks of the user settings). Managed settings apply to
   every user of the host, so `awb hook` exits 0 at once for every user but `work_user` of `/etc/awb/paths.conf`:
   the owner's own sessions are not checked. The deny rule for connectors (`mcp__*`) is not a managed setting,
   because it would take the connectors of the owner's own sessions too; it stays in the immutable settings of
   the work user, and no project file can allow what a user file denies.

Home folders come from `getent passwd`. No home path is written into any file of this folder.

## Needs

bash, coreutils, util-linux (`runuser`, `mount`, `mountpoint`), e2fsprogs (`chattr`, `lsattr`), diffutils
(`cmp`), `useradd` and `gpasswd`, systemd, git, gpg, python3 with venv and setuptools, poppler (`pdftotext`,
`pdfinfo`).

## Limits

- The hooks and the deny rule are a guard rail for honest sessions. The seal itself is the file modes: the work
  user cannot read the vault whatever a session does. A session that starts a client of its own with another
  configuration folder leaves the work user's settings behind (the managed hooks still run).
- The commit gate of a project is a session-owned control: `--no-verify` or another `core.hooksPath` skip it.
  It holds nothing against a hostile session; push stays off, so a committed name stays on the host.
- The deny rule `mcp__*` bounds what a session takes in through connectors, not what it sends out: the Bash
  tool and the web tools can reach the network. Egress is out of the seal's scope; a session holds sanitised
  text only.
- `chattr +i` needs a file system that keeps the flag (ext4, xfs, btrfs). `verify.sh` reports a FAIL when the
  flag is missing.
- A project moved by `setup.sh` gets a new path and so a new memory key of the client. Notes the client kept
  under the old key stay with the owner.
- A mirror folder must be readable for others (mode 755 folders, 644 files), else the work user sees the mount
  but cannot read it.
