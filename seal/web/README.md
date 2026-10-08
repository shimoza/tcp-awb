# The web side

The web console reaches the Workbench through one gateway. Its handlers live in `awb/tcp/web/`, its contract in
`docs/api/openapi.yaml` (`awb api check`). This folder holds the systemd templates that run them from
`/opt/tcp-awb`, the code that `setup.sh` installs. `setup.sh` does not install these units: the switch from the
copies in `/opt/awb-web` is a step of its own, done once, by the owner.

| unit | user | what it runs | listens on |
|---|---|---|---|
| `awb-web.service` | `awb-web` | `awb/tcp/web/gateway.py`: sign-in, sessions, the checks of every request | the two sockets below; 127.0.0.1:8080 and 8081 for one release |
| `awb-web.socket` | root, group of the tunnel's user, 0660 | the front door: the gateway accepts a peer only when its uid is the tunnel's user (`SO_PEERCRED`) | `@FRONT_SOCKET@` (folder root 0750, same group) |
| `awb-web-status.socket` | root, 0666 | GET /health and nothing else, for `awb web status`, the publish code and the deploy | `/run/awb-web-status.sock` |
| `tunnel/cloudflared.conf` | the tunnel's user | cloudflared's drop-in `cloudflared.service.d/50-awb-fence.conf`: its own user, root copies the token into its runtime folder (0700) and loads the fence first, metrics pinned to 127.0.0.1:20241 | |
| `tunnel/tunnel-fence.nft` | root | `/etc/awb/tunnel-fence.nft`: the tunnel's user opens no new loopback TCP connection but the resolver (53) and, for one release, 8080 and 8081; nobody else reaches its metrics port | |
| `awb-console-data.service` | `awb` | `awb.tcp.web.projects_api`: the projects, read only | 127.0.0.1:8182 |
| `awb-console-tenants.service` | `awb-console` | `awb.tcp.web.tenant_api`: the test tenants, read only; the key service lets this user alone read without a project (F2) | 127.0.0.1:8183 |
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
| `@OWNER_HOST@` | `owner_host` of `/etc/awb/paths.conf`: the host name of the owner level, set once with `sudo awb web host HOST` |
| `@FRONT_SOCKET@` | `front_socket` of `/etc/awb/paths.conf` (default `/run/awb-web/front.sock`) |
| `@FRONT_DIR@` | the folder of the front socket |
| `@CLOUDFLARED_USER@` | `cloudflared_user` of `/etc/awb/paths.conf` (default `cloudflared`), a system user `install.sh` makes once |

A missing `owner_host`, `front_socket` or `cloudflared_user` stops `install.sh` before anything changes, so a deploy
never renders a unit without them (RT-04). `seal/setup.sh` keeps the three when it writes the host file again.

## The switch (production stays on `/opt/awb-web` until the owner runs it)

1. `sudo seal/setup.sh` installs the committed code into `/opt/tcp-awb/releases/<commit>` (`git archive HEAD`)
   and points `/opt/tcp-awb/src` at it.
2. Publish the reviewed page of the console to `/srv/awb-web/index.html`, the old one kept as
   `index.html.before-switch`.
3. `sudo seal/web/install.sh --domain <host name of the site>`, first with `--dry-run`. It creates the system user
   `awb-console` once, renders every template with the values of `/etc/awb/paths.conf`, keeps every unit it replaces
   as `<unit>.before-switch`, retires `awb-ask.service.d/90-awb-web.conf` and restarts the key service and the web
   units.
4. As the owner: `awb keys unlock`. The restarted key service holds no key until then.
5. Check: every unit is active, the console signs in, the projects, the tenants and the create form (Query, Project)
   load.

To go back: `sudo seal/web/install.sh --rollback`, and `sudo awb web publish --rollback index.html.before-switch`.

A new page of the console goes live with `sudo awb web publish --from-queue ID` (or `FILE`): checked, the old page
kept as `index.html.before-ID`. `awb web status` shows the page, the last backup and the gateway.

The logins of the console live in `/etc/awb-web/users.json` (`sudo awb web user add LOGIN [--level owner]`, `reset`,
`remove`, F4 and T9). Until that file exists the single login of `auth.json` works. A reader login waits 15 minutes
after 5 failures within 15 minutes, doubled for each further series up to 24 hours; 30 failures within an hour pause
every reader sign-in. An owner entry is never locked: each failure moves its next try 1, 2, 4 ... 60 seconds ahead.
One address (the /64 of an IPv6 address) gets 8 failures a minute and one sign-in at a time; the failure is counted
before the PBKDF2 runs. A sign-in waits up to 3 seconds for one of four PBKDF2 slots and answers "busy" (503, not
counted) when none came free; at most 64 requests without a session are open at once; a login form is taken once;
a login keeps at most 5 sessions and a full table of 128 refuses a new one. The gateway logs every attempt (time, login, result) to `/var/lib/awb-web/signin.log` and
rebuilds the waits from it after a restart; `sudo awb web status` reads it.

## The front door (T9 step 1)

The gateway takes the site on the front socket only. Only the tunnel's own user may open it: the socket is root's
with that user's group, and the gateway checks the peer's uid before it reads a byte. cloudflared runs as that user
(the drop-in of `tunnel/`); before it starts, root loads the fence, so that user opens no new loopback TCP
connection and a changed ingress reaches nothing but the front socket. `install.sh` restarts cloudflared when the
drop-in or the fence changed.

The order of the switch: the deploy brings the sockets and the fence while the gateway still listens on 8080 and
8081 (and the fence still lets the tunnel reach those two). Then, in the Cloudflare dashboard, both ingress rules of
the tunnel (the site and the `/ask` path) point at `unix:<front_socket>`. The release after drops `--ports` from
`awb-web.service` and the two ports from the fence.

After the switch `--domain` may be left out: the script reads the host name from the installed `awb-web.service`.
`sudo awb deploy` runs `releases/<commit>/seal/web/install.sh --only UNIT...` from the release it extracted, once an
installed web unit points at `/opt/tcp-awb`: the script renders every template as before and restarts the named
units only, in its own order.

The web adapters in `/opt/awb-web` keep working against a newer `/opt/tcp-awb` until the switch: the private
names they call (`register._check`, `projects._known_tags`, `projects._locked`, `obs._text`, `obs._child`,
`obs._local`, `obs.Client._sign`, `obs.Client._url`, `sweep._tags`) stay as aliases of the public ones. After the
switch these aliases can go.
