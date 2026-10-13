# F2: threat model of the Architect Workbench, 2026-10-13

Scope: the whole system as the repository describes and implements it at commit 8a0f44c: the READMEs,
`INTERFACES.md`, the units and scripts of `seal/`, the egress rules and the code. Nothing ran on a host. The review of 2026-10-13 is taken as known: its open
items E1, E2, B3, B4, B5, B7, K1, T4, T5 and D1 are counted where they bear on a gap and not restated.

## The system in one view

Two kinds of control hold the system together. Boundaries the kernel enforces: nine uids with their file modes, the
Unix sockets with their groups and peer uid checks, the systemd sandboxing of the services and the task units, the
nftables table of the uid `awb` in the mode enforce, the loop image of the build side, the immutable client settings
and the front socket of the gateway. Rules and hooks, which hold for an honest session and fail for a hostile one:
the six client hooks and the deny list, the commit gate in a repository's hooks, the work rules, the system prompts
of Ask and the chat, the task block of the build queue and the owner's own reading of a diff, a result file or a
pull request. The seal README says this of the hooks and the gate; this model asks it of every control.

## Assets

| asset | where it lives | who reaches it by design |
|---|---|---|
| the name register and the vault | the owner's `tcp-vault`, 700, decrypted in the vault daemon | the owner, the vault daemon, the customers and materials services (owner uid); sessions get a yes or no |
| the owner's tenant keys and secrets | the password store, 700; the key service's memory | the owner, the key service; sessions get signed calls and a lease of a raw key for at most 60 minutes |
| the model credentials | the Ask key (awb-ask, 400); the build copy (owner, 400) or the build user's login; the work user's own login | awb-ask; each task home for the task's clock; the work user |
| customer material after intake | project `input/`, the outbox, the project's `in/` in the owner bucket, the materials store | the work user, awb-ask, every reader of the console through the chat |
| the knowledge base | `tcp-kb` of the work user, git without a remote, group `awb` | every session through `awb kb add`; awb-ask reads; the dataset build exports |
| the public dataset and its MCP server | `tcp-shared/datasets`, the owner bucket, the public dataset repository | the owner publishes with `gh`; strangers run the server |
| the repository and its push path | GitHub, `main.git` on the build side, the owner's working copy | the owner, the runner (owner uid, the owner's `gh` credentials), cloud sessions on branches |

## Actors

### A project session, uid awb, on a public model

Reach today: its project and every other project of the work user (one uid owns them all), the shared tree, the
knowledge base through `awb kb add`, the check socket, the key service (read key from an active granted project,
lab key writes with the project code, a lease of a raw key), the lab inbox and the project's `in/`, puts into
`from-session/` after the name check and the send gate. The network: everything in the mode count, the allow list
and DNS in the mode enforce.

Boundaries: the file modes keep it out of the vault, the key files, the owner's home and the build root. The key
service refuses IAM writes, writes without a project code, the exchange tenant's lab key and the originals of the
owner inbox. nftables in the mode enforce.

Rule only: the pre-tool guard, the write hooks, the commit gate, the deny list and the work rules. A hostile
session, which is what a session becomes once a planted instruction steers it, keeps: its own
`~/.ssh/authorized_keys` and its crontab (persistence past the session), the knowledge base, every resource of a
granted tenant (the lab key is tenant wide; the project code is a label on the call and not a scope on the
resource), the model API and the telemetry intake as channels out and in the mode count every host.

### A build task, uid awb-build

Reach: its clone, the venv read only, `/tmp`, the model endpoint, the token host in the mode login, the upstream
resolvers on every port (B4) and its copy of the credential. It hands over commits in a bundle.

Boundaries: the unit (no network but the allow list, `ProtectSystem`, `/etc/awb` inaccessible, the other clones
hidden), a uid outside the group `awb`, the loop image, the runner judging in `main.git`, the gate with the register
over every commit and the pinned client. The `paths` of the block are checked by the runner, so that rule is
mechanical.

Rule only: the hooks of the build settings, the prompt and the owner's reading of `result.md`. D1 stands: a task
can forge its green. A hostile task keeps code on `main.git` and on GitHub `main` after a forged green. It keeps
the credential for its clock plus whatever B4 lets out.

### A cloud session that opens pull requests

Reach: the repository, the internet, a branch pushed with the hooks off. It holds no secret of the host. It reads
the repository's instructions and any issue text, an injection surface of its own.

Stopped by the owner alone: a reading of the diff, the gate in the owner's session, the merge. The repository has
no workflow and no required status check, so nothing runs on the GitHub side. The procedure has three weak points.
`CLAUDE.md` asks to run the tests: a branch's tests and its `conftest.py` run with the owner's uid and the real vault
readable. The branch can change the guard that the review of 2026-10-13 added. A merge made in the GitHub web
interface meets the gate only when the owner next pulls. After the merge the deploy runs the merged `setup.sh` as
root.

### Web console users

A reader signs in with a password (PBKDF2, the limiter) and holds an 8 hour cookie: every project's state, the
board, the tenant inventory in codes, the chat over the sanitised inputs of any project and the Ask API on a shared
daily budget. The owner level adds TOTP, a 15 minute idle limit, customers, projects, uploads into `in/` and publish.

Boundaries: the gateway's peer checks and the socket owners, one uid per service (owner-actions has no network and
reads one file), the CSRF and Origin rules, Cloudflare in front. Rule only: nothing scopes a reader to a project.
Sanitised copies still carry the substance of a customer's requirements. The dataset's own warning says that a
capable model guesses a customer from them. A reader is a trusted colleague by design and not an outsider. One reader
can spend the whole daily Ask budget.

### A dataset user who runs the MCP server

Reaches nothing of the host. The risk runs the other way: the user runs `tcp_facts_mcp.py` or the one click
bundle from a GitHub release and trusts the owner's `gh` account. The server was reviewed (M1 to M3). The release
lists sha256 sums but carries no signature and the tag is not signed. Whoever holds the owner's `gh` credentials can
ship a new server to every user. The runner holds them, since it runs as the owner and pushes with them.

### An outside attacker on the internet

Entry points: the tunnel to the gateway (a sign-in only; the owner host needs TOTP), sshd (the owner's keys are
copied to the work user, so one lost laptop key opens both accounts), a pull request or an issue on GitHub, a
customer file through the owner inbox, the public documentation repositories the refresh clones and the model API
answers (influence through what the model reads, nothing else). Every entry meets a uid boundary or the gateway.
The rule-only part is what the owner runs on a stranger's branch and what the owner's uid parses.

### A compromised dependency

The runtime is the standard library and poppler. Poppler parses hostile PDFs under the owner's uid: the intake, the
materials import and the lab inbox check inside the key service. That is the one place where raw bytes from outside
meet the owner's uid in a C parser. The test extras run under the owner's uid when the owner runs the suite. The
work user's client updates itself nightly from the vendor; the build client is pinned by sha256, which is the
better of the two. Every venv carries the system site packages.

## The six points of attention

1. Pull requests from cloud sessions: the chain is branch, review, local gate, merge, root deploy. The two
   mechanical steps run inside the owner's uid on code the branch wrote.
2. The deploy: `sudo awb deploy` turns HEAD of the owner's copy into root code (`setup.sh --update`, the units).
   The plan is validated in shape and not in content. No gate and no suite run inside the deploy. The refusal of a
   copy behind the live release is sound. The deploy's safety is a property of the owner's reading before the
   commit and not of the deploy.
3. The egress filter in enforce: it covers the uid `awb` alone. Unfiltered: the owner (the key service, the vault
   daemon, the runner, the materials and customers services), awb-ask (the strong model with inputs), awb-web,
   awb-console and root. E1 and E2 are open. The allowed hosts and DNS remain ways out, as `ALLOW.md` says. The
   filter ships counting; whether it enforces today is not visible from the code.
4. The build runner's key mode: the build key is by default a copy of the Ask page's key, so a leak burns both and
   the Ask budget with them. In both modes the credential lies in the task home for the task's clock. The gate's
   token pattern `sk-` catches it in a commit and the result fields are fixed, so the open exits are B4 and the model
   API itself. In the mode login the credential is the owner's subscription login, worth more than a key: it is the
   owner's account.
5. Ask on a strong model with tools: the three tools are read only, their arguments are cut and the price host is
   fixed. Injected text in an input or a fact can distort an answer and show a reader what the reader may read
   anyway, plus the tenant inventory. The confidentiality effect is low. The integrity effect is real: a planted fact
   cited with its id looks checked. The name screen stops registered names before the question leaves.
6. Prompt injection through customer material, the knowledge base or tool results: the wipe removes names and not
   instructions. Three sinks differ in reach. The project session has a shell and the full reach of the first actor.
   The chat and Ask are bounded by their tools. The build task reads its clone, so instructions planted in a file
   by an earlier task or pull request steer the next one. The knowledge base closes a loop: a session adds a fact
   with a fabricated source, the refresh exports it to the public dataset and Ask cites it.

## The gaps, ranked

Impact first, then likelihood. "Host" says whether the owner's host is needed beyond a code change.

| # | gap | impact | likelihood | control that closes it | host |
|---|---|---|---|---|---|
| G1 | a pull request's tests run under the owner's uid with the vault readable | high | medium | the gate and the suite of a foreign branch as `awb-build` in a task unit (`awb build check BRANCH`); never `pytest` of an unreviewed branch in the owner's shell | yes |
| G2 | the deploy makes HEAD root code without a mechanical gate | high | low to medium | the deploy runs the gate over deployed..target and the suite in the build unit first; it shows the diff of `seal/`, `awb/deploy.py`, `awb/buildrun.py` and `hooks*/` and asks once | yes |
| G3 | a hijacked session persists through the work user's `authorized_keys` and crontab | high | medium | `AuthorizedKeysFile` of sshd pointing at a root owned path; `cron.deny` for `awb`; the two nightly jobs as root timers with `User=awb` | yes |
| G4 | the lab key is tenant wide, so a session deletes any resource of a granted tenant | medium to high | medium | one tenant per project; or writes only through a lease whose policy carries a tag condition on the project code; or a write allow list by service and path in `keys.conf` | yes |
| G5 | poppler parses hostile PDFs under the owner's uid | high | low to medium | run `awb.extract` in a transient unit as a throwaway user with no network and the file on a pipe; the build side shows the form | yes |
| G6 | knowledge base poisoning reaches the public dataset | medium | medium | `awb kb review` lists the facts added since the last release with their sources; the dataset build refuses unreviewed ones | no |
| G7 | the dataset release path has no signature and shares the owner's `gh` credentials with the runner | high for strangers | low | signed tags and an attestation for every release; a fine grained token for the dataset repository alone; publish from the owner's terminal only | yes |
| G8 | egress covers one uid of nine | medium | low to medium | a table for `awb-ask` (the model and the price host) and one for the owner's services with their known peers, counting first | yes |
| G9 | the build credential is the owner's login or the Ask key | medium to high | low to medium | a dedicated key with a monthly cap for the build; close B4 first | yes |
| G10 | readers see every project | medium | low | a project list per reader in `users.json`, enforced by the gateway | no |
| G11 | the runner pushes to GitHub before the owner reads anything (D1) | medium | low to medium | `build_push = no` until D1 is closed; or a push to `build/ID` with `main` moved by the deploy `--task` alone | no |

Two notes. The owner's ssh keys on both accounts (step 8 of the setup) double the value of one lost key. The lease
of a raw key is harmless while the TCP API is refused for the uid `awb` in the mode enforce; the day those hosts
enter the allow list for Terraform, a hijacked session uses a leased key for an hour, so the lease policy should be
narrow before that day.

## Five things to do first

1. Lock the work user's persistence (G3): `authorized_keys` to a root owned path, `cron.deny`, root timers for the
   two nightly jobs. One hour on the host and nothing in the code.
2. Stop running a foreign branch's suite as the owner (G1): a `build check` of a branch in the task unit. The rule
   in `CLAUDE.md` then names it. This also gives G2 its mechanical step.
3. Sandbox the extractors (G5): one transient unit, the pattern of the build side, used by the intake, the materials
   service and the lab inbox check.
4. Make the egress filter count for something (E1 and G8 with B4): close E1, enforce, then a counting table for
   `awb-ask` and the owner's services. After that narrow the lab key (G4).
5. Protect what strangers run (G7 and G11): signed tags and attestations for the dataset, a separate token for it
   and `build_push = no` until D1 has a fix.

## What this model could not judge from the code

- The sudoers rule the runner needs for `sudo -n systemd-run` is not in the repository. Its breadth decides whether
  the runner's uid is the owner or the owner with root.
- Whether the egress filter enforces today and what the counters have shown.
- The Cloudflare Access policy in front of the tunnel and whether sshd listens on the internet.
- The GitHub settings of both repositories: branch protection, who may push, two factor sign-in, visibility. Whether
  the owner merges on GitHub or locally after the gate; the history shows merge commits and nothing more.
- Whether the readers are the owner alone or several people.
- The crontab of the work user: `ALLOW.md` names two jobs, `setup.sh` writes none.
- The IAM rights behind the read and lab keys on each tenant. G4 depends on them.
- The versions of poppler and the system packages and the unattended upgrade settings of the host.
