# The web side

The web console reaches the Workbench through one gateway. Its handlers live in `awb/tcp/web/`, its contract in
`docs/api/openapi.yaml` (`awb api check`). This folder holds the systemd templates that run them from
`/opt/tcp-awb`, the code that `setup.sh` installs. `setup.sh` does not install these units: the switch from the
copies in `/opt/awb-web` is a step of its own, done once, by the owner.

| unit | user | what it runs | listens on |
|---|---|---|---|
| `awb-web.service` | `awb-web` | `awb/tcp/web/gateway.py`: sign-in, sessions, the checks of every request | 127.0.0.1:8080 and 8081 |
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

To go back: `sudo seal/web/install.sh --rollback`, and the page backup copied over `/srv/awb-web/index.html`.

After the switch `--domain` may be left out: the script reads the host name from the installed `awb-web.service`.
`sudo awb deploy` runs `releases/<commit>/seal/web/install.sh --only UNIT...` from the release it extracted, once an
installed web unit points at `/opt/tcp-awb`: the script renders every template as before and restarts the named
units only, in its own order.

The web adapters in `/opt/awb-web` keep working against a newer `/opt/tcp-awb` until the switch: the private
names they call (`register._check`, `projects._known_tags`, `projects._locked`, `obs._text`, `obs._child`,
`obs._local`, `obs.Client._sign`, `obs.Client._url`, `sweep._tags`) stay as aliases of the public ones. After the
switch these aliases can go.
