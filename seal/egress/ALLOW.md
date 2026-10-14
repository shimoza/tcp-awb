# The allow list of the work user

What the work user (`awb`, every working session, the portal, the console data and the project creation service)
may reach on the network. `awb egress load` (root) reads the rows in backticks from this file, resolves the host
names and puts their addresses into the sets of `work-egress.nft`. Every other new connection of the work user is
counted by class and, in the mode enforce, refused. Measured on 2026-10-11 on the sealed host; how is at the end.

## Allowed: a new connection on tcp 443 passes

| destination | port | set | why |
|---|---|---|---|
| `api.anthropic.com` | `tcp 443` | `allow` | the model API of every session; the client also checks a WebFetch domain and fetches its feature flags here. Seen on every sample of the work user |
| `claude.ai` | `tcp 443` | `allow` | the client's account login (the work user signs in with a claude.ai account) |
| `platform.claude.com` | `tcp 443` | `allow` | the client's token exchange and refresh, also for claude.ai accounts (the nightly run renews the login) |
| `downloads.claude.ai` | `tcp 443` | `allow` | the nightly `claude update` of the work user (`awb-work-update.timer` 01:35 UTC, a root owned timer in place of its crontab since F2 G3): the client is the native install, its version check and download go here |
| `http-intake.logs.us5.datadoghq.com` | `tcp 443` | `allow` | the client's operational telemetry, on by default and not switched off in the work user's settings. Seen on the first sample |
| `browser-intake-us5-datadoghq.com` | `tcp 443` | `allow` | the client's error reports, same switch as the telemetry |
| `calculator.otc-service.com` | `tcp 443` | `allow` | the public price API of T Cloud Public: `awb price` and the skill tcp-price run in the sessions, no key |

Allowed without a row, because they never leave the host:

- loopback (`lo`): the resolver `127.0.0.53` (the work user's only DNS), the portal, the console data and every other
  local service. The tunnel's fence keeps its own user off loopback; the work user keeps it.
- Unix sockets, which nftables does not see: the name check (`/run/awb/check.sock`), the key service
  (`/run/awb-keys/cloud.sock`), the console sockets of the web side.
- the established packets of a connection the allow rules accepted (they carry the conntrack mark `0xa0b001`), so an
  allowed connection goes on. An established packet without the mark (a connection opened outside the list while
  the filter counted or an answer of a listener of the work user to an inbound connection) passes uncounted in the
  mode count and is refused in the mode enforce. `sudo awb egress enforce` deletes the conntrack entries of the work
  user's connections outside the list (mark `0xa0b002`) when `conntrack` is installed; without it their packets are
  refused all the same until the entries time out.

## Watched: counted in a class of their own, never allowed

| destination | port | set | why |
|---|---|---|---|
| `iam.eu-de.otc.t-systems.com` | `any` | `tcp_api` | the TCP API: the work user reaches it through the key service only |
| `ecs.eu-de.otc.t-systems.com` | `any` | `tcp_api` | as above |
| `evs.eu-de.otc.t-systems.com` | `any` | `tcp_api` | as above |
| `vpc.eu-de.otc.t-systems.com` | `any` | `tcp_api` | as above |
| `elb.eu-de.otc.t-systems.com` | `any` | `tcp_api` | as above |
| `nat.eu-de.otc.t-systems.com` | `any` | `tcp_api` | as above |
| `dns.eu-de.otc.t-systems.com` | `any` | `tcp_api` | as above |
| `ims.eu-de.otc.t-systems.com` | `any` | `tcp_api` | as above |
| `obs.eu-de.otc.t-systems.com` | `any` | `tcp_api` | the buckets, as above (a bucket name resolves to the same addresses) |
| `iam.eu-nl.otc.t-systems.com` | `any` | `tcp_api` | the second region, as above |
| `ecs.eu-nl.otc.t-systems.com` | `any` | `tcp_api` | as above |
| `obs.eu-nl.otc.t-systems.com` | `any` | `tcp_api` | as above |
| `100.125.0.0/16` | `any` | `tcp_api` | the service network the eu-de API names resolve to from this host (measured), with the cloud's resolvers and NTP |
| `github.com` | `any` | `github` | the client refreshes its plugin marketplace from here; no project of the work user has a remote, so nothing pushes |
| `api.github.com` | `any` | `github` | as above |
| `codeload.github.com` | `any` | `github` | as above |
| `raw.githubusercontent.com` | `any` | `github` | the client's changelog feed after an update |
| `objects.githubusercontent.com` | `any` | `github` | as above |

The TCP API and the buckets are not on the allow list. The work user reaches a test tenant only through the key
service (`/run/awb-keys/cloud.sock`), which runs as the owner (`User=@OWNER@` in `seal/awb-keyd.service`), so its
connections carry the owner's uid and never enter the chain of the work user. `seal/verify.sh` proves it on the host
(the key service's process runs under another uid, and a planted connection of the owner's uid leaves the counters
alone); `tests/test_egress.py` proves it on the rendered rules (the base chain sends the work user's uid alone into
the chain work). One path is open by design: the key service's op `lease` hands a session a temporary key of a
granted project for at most 60 minutes (Variant B of `presentations/console-security/DESIGN-terraform.md`). Used
from the work user's uid, that key counts as `tcp_api` and is refused in the mode enforce. No command of the
Workbench uses a lease from a session yet (`awb cloud terraform` is designed, not built); when it comes, its hosts
move into the allowed table.

## The classes of a connection outside the list

First match wins, in this order. `awb egress status` shows the counts of the last 24 hours per class.

| class | what |
|---|---|
| `probe` | 192.0.2.0/24 and 2001:db8::/32 (documentation blocks): the planted connection of `seal/verify.sh`; never stops `enforce` |
| `metadata` | 169.254.0.0/16 and fe80::/10: the cloud's metadata service and other link-local addresses |
| `dns` | port 53 to any address but loopback: a resolver other than the host's own |
| `ntp` | udp 123: the host's time comes from systemd-timesyncd under its own user, never from the work user |
| `tcp_api` | the watched TCP API rows above |
| `github` | the watched GitHub rows above |
| `ssh` | tcp 22 |
| `mail` | tcp 25, 465 and 587 |
| `web` | tcp 80 and 443, udp 443: WebFetch, curl, pip, npm and every web host not listed |
| `other_tcp`, `other_udp`, `icmp`, `other` | the rest |

## Not on the list, and why

- `registry.npmjs.org`: the client of the work user is the native install (`claude update` goes to
  `downloads.claude.ai`); no session installs npm packages, plugins or `npx` servers.
- `storage.googleapis.com`: the native updater of versions before 2.1.116 and the install counts of `/plugin`; the work
  user runs 2.1.296 and has no plugin.
- `mcp-proxy.anthropic.com`: the connectors of the account, denied to the work user (`mcp__*`). It resolves to the
  address of `api.anthropic.com` today, so it passes with it (see the limits).
- `code.claude.com`: the client's documentation lookups; optional. Counted as `web`.
- the owner's bucket, the exchange buckets and the mirrors of the documentation: reached by the owner and the key
  service, never by the work user.

## How it was measured

- `ss -tunape` once and then every 2 seconds for 40 minutes (11:23 to 12:03 UTC) on 2026-10-11, the peers of uid `awb`
  and `awb-ask`: `api.anthropic.com` on every sample and `http-intake.logs.us5.datadoghq.com` on the first one;
  nothing else, no UDP but the resolver on loopback. The mode count is the longer measurement.
- The client's own list of the hosts it needs (code.claude.com/docs/en/network-config, read on 2026-10-11) against the
  work user's install: native, version 2.1.296, no plugin installed, the official marketplace known, `autoUpdates` off.
- The crontab of the work user: `claude update` at 01:35 and the commit of the knowledge base at 01:40 (no remote).
  Since F2 G3 both run from root owned timers as the work user (`seal/work-jobs/`) and the work user has no crontab.
- The projects of the work user: no git remote. The units that run as the work user (`awb-portal`,
  `awb-console-data`, `awb-project-create`): loopback and Unix sockets only.
- `/etc/resolv.conf`: the stub `127.0.0.53`. The time: systemd-timesyncd.

## Limits

- An allowed host is a way out. `api.anthropic.com` takes uploads under any account's key, the Datadog intake takes
  any text, and every Anthropic host that shares the address of `api.anthropic.com` passes with it. The filter bounds
  the ways out to a short list; it does not close them.
- DNS through the host's resolver stays open: a session can encode data in names it looks up.
- The filter sees the output alone. A packet from outside still reaches a listener of the work user (and, with SYN
  cookies under a flood, a handshake can complete since the SYN-ACK then carries no socket); in enforce nothing the
  listener sends back leaves the host. An input rule cannot tell the work user's listener from another before the
  socket lookup, so there is none.
- The sets hold addresses, not names. A listed host that moves to an address it never gave before is counted as
  `web` until the next refresh (5 minutes); addresses seen in the last 7 days stay in the set.
- A boot loads the table before the logins and the units of the work user; when that load fails the work user is not
  filtered until the next run of the timer, and `seal/verify.sh` and `awb seal check` say so.
