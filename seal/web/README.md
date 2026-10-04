# The web side

The web console reaches the Workbench through one gateway. Its handlers live in `awb/tcp/web/`, its contract in
`docs/api/openapi.yaml` (`awb api check`). This folder holds the systemd templates that run them from
`/opt/tcp-awb`, the code that `setup.sh` installs. `setup.sh` does not install these units: the switch from the
copies in `/opt/awb-web` is a step of its own, done once, by the owner.

| unit | user | what it runs | listens on |
|---|---|---|---|
| `awb-web.service` | `awb-web` | `awb/tcp/web/gateway.py`: sign-in, sessions, the checks of every request | 127.0.0.1:8080 and 8081 |
| `awb-console-data.service` | `awb` | `awb.tcp.web.projects_api`: projects and tenants, read only | 127.0.0.1:8182 |
| `awb-project-create.socket` and `.service` | `awb` | `awb.tcp.web.create_api projects`: new projects | `/run/awb-project-create.sock` |
| `awb-customers.socket` and `.service` | the owner | `awb.tcp.web.create_api customers`: new customers, the register | `/run/awb-customers.sock` |
| `awb-materials.socket` and `.service` | the owner | `awb.tcp.web.materials_api`: copy-only imports from the buckets, read through the key service; no network of its own | `/run/awb-materials.sock` |
| `awb-ask.service.d/95-project-chat.conf` | `awb-ask` | `awb.tcp.web.chat_service`: the project chat and the Ask page | 127.0.0.1:8181 |
| `awb-portal.service.d/90-awb-web.conf` | `awb` | the portal, moved to 8180 | 127.0.0.1:8180 |

Owner, work user and chat stay separate processes, each with its own user and only its own rights. The sockets
belong to `awb-web`, so only the gateway reaches them.

## Placeholders

| placeholder | value |
|---|---|
| `@OWNER@` | the owner user (`owner` of `/etc/awb/paths.conf`) |
| `@DOMAIN@` | the host name of the site, without scheme |
| `@VAULT@` | `vault` of `/etc/awb/paths.conf` |
| `@SHARED@` | `shared` of `/etc/awb/paths.conf` |
| `@WORK_HOME@` | the home folder of the work user |

## The switch (not done yet: production stays on `/opt/awb-web` until the owner decides)

1. Install the current code: `sudo seal/setup.sh` (it installs `/opt/tcp-awb/src` from `git archive HEAD`).
2. Render the templates into `/etc/systemd/system/`, one file each, with the values above:

   ```bash
   sed -e "s|@OWNER@|$OWNER|g" -e "s|@DOMAIN@|$DOMAIN|g" -e "s|@VAULT@|$VAULT|g" -e "s|@SHARED@|$SHARED|g" -e "s|@WORK_HOME@|$WORK_HOME|g" seal/web/awb-web.service | sudo tee /etc/systemd/system/awb-web.service
   ```

   The two drop-ins go into `/etc/systemd/system/awb-portal.service.d/` and `awb-ask.service.d/`. The older
   drop-in `awb-ask.service.d/90-awb-web.conf` goes: `95-project-chat.conf` replaces it.
3. `sudo systemctl daemon-reload`, then restart `awb-keyd` (it forgets the keys: run `awb keys unlock` as the owner
   right after, the materials service reads the buckets through it), `awb-web`, `awb-console-data`, `awb-ask`,
   `awb-portal` and the three sockets with their services.
4. Check: the console opens and signs in, every page loads, `systemctl status` shows each unit active.
5. Keep `/opt/awb-web` until the check passed. To go back, reinstall the units that point there and restart.

The web adapters in `/opt/awb-web` keep working against a newer `/opt/tcp-awb` until the switch: the private
names they call (`register._check`, `projects._known_tags`, `projects._locked`, `obs._text`, `obs._child`,
`obs._local`, `obs.Client._sign`, `obs.Client._url`, `sweep._tags`) stay as aliases of the public ones. After the
switch these aliases can go.
