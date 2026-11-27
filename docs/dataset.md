# The knowledge as a dataset

How the knowledge base leaves as the public dataset TCP Facts. Back to the [README](../README.md).

## The knowledge as a dataset

The knowledge base of TCP facts is not part of this repository. It leaves as the dataset "TCP Facts", built in one
command and again at the end of every refresh:

```bash
awb dataset build
```

That writes `~/tcp-shared/datasets/tcp-facts-<date>/`, its zip and `tcp-facts-<date>.mcpb` (the one-click extension for Claude Desktop): `facts.md` with every fact on one line, grouped
by topic and small enough for one chat, one file per topic under `topics/`, `facts.jsonl` with the metadata, the
list of orderable services, a `PROMPT.md` and a `HOW-TO.pdf`. A fact leaves the dataset only when something invalidates it (retired or superseded, refuted by a recheck, a service the service description no longer offers), never for its date: an expired fact stays, marked `recheck due` with its check date in `facts.md` and counted in the freshness line of the README. A contract fact whose source cites the current revision of the service description (or cites none and was checked on or after it) is not due for its date while that revision is current; a new revision requests its recheck. Source notes of the import of 2026-10-20 leave as "earlier private knowledge base", not "old setup". Every fact carries its validation level, from its grade, its check block and its check date alone: V4 kept live (a live fact whose read or write block the weekly recheck takes), V3 live, V2 contract, V1 docs and V0 recheck due (past its check date). It is `level` in `facts.jsonl`, `(KB-XXXX, V3 live, checked DATE)` in `facts.md` and `topics/`, the counts in `MANIFEST.json` and the table "Validation levels" in the README, PROMPT.md and HOW-TO.pdf, two plain lines per level with its count. No level stands for a confirmation from outside. The dataset holds no price list (no `prices/` folder, no
price file in `MANIFEST.json`): prices come live from the public price API with `--live` of the MCP server. Every text file
opens with the rules for the assistant that reads it, so a person attaches `facts.md` to ChatGPT, Claude or a local
model and types `Use the attached TCP Facts. Answer: <question>`. The dataset carries a best-before date of 30 days. Its README, PROMPT.md and HOW-TO.pdf warn against customer data in prompts to a public model.
The README opens with a first screen for a reader of the public repository: one sentence what it is and for whom,
the three ways to use it (attach `facts.md`, the one-click `tcp-facts.mcpb`, `claude mcp add`), "What this proves"
(the facts, the facts tested live, the services and the weekly releases, counted from the date tags of the local
clone, each with the jq or gh command that checks it), one example fact, one freshness line (the facts with a recheck
due and the jq command that lists them, the facts with a weekly automated check, level V4, and the check periods of
the rest), the release date and the licence line. It holds no table of fact ids, names only files of the repository
and calls `MANIFEST.json`, `HOW-TO.pdf` and the `.mcpb` release assets. `STATS.json` (B-114), written beside
`facts.jsonl` and published with it, holds those numbers (release date, facts, facts tested live, services covered and
orderable, facts with a weekly automated check, facts whose recheck is due, the licence of data and code), each with
`counted_by`, the command that counts it again; the README reads its numbers from the same values. The facts are CC BY 4.0; `tcp_facts_mcp.py`
carries an Apache-2.0 header and every build carries `LICENSE-CODE`, the Apache License 2.0 of this repository.
Its README, PROMPT.md and HOW-TO.pdf also carry "What the facts cover", counted from the facts at each build: how
many orderable services per section of the service description have a plain fact that says what the service is and
does (a statement that opens with the service's name), the requirement areas (facts that open with "T Cloud Public
(TCP)" and a verb, each with its id), an example of checking a requirement and citing the fact id, and the contract
matters the dataset never holds (certificates and attestations, SLA values, support hours and response times, prices
and subcontractors as commitments), then how many facts name a certificate or an attestation as a public source lists
it and in which topics.
How much the dataset helps a model is measured with the evaluation set in `eval/tcp-facts/` (50 questions with expected
answers and fact ids, a scorer and a runner that asks a model with and without the facts; see its README).
The files and the PDF metadata say who compiled them: `TCP Facts, github.com/shimoza/tcp-facts`, never a person.
Its README, PROMPT.md and HOW-TO.pdf also say where the facts come from: how many documentation facts come from the
repositories of the GitHub organisation opentelekomcloud-docs (Apache License 2.0, with an attribution line in the
licence section), that the documentation website is not crawled, that no community portal or internal
documentation is used and that the live checks ran on the compiler's own test tenants.
Its README and PROMPT.md carry the section "MCP clients" with the configurations of the next part, HOW-TO.pdf the same.
Before a release the owner reads what changed: `awb kb review` lists every fact of the built folder that was added,
amended (statement, source or grade) or retired since the last release, with its id, grade, check date, source and
statement, compared with `RELEASED.jsonl` beside the folders (the `facts.jsonl` of the last release, written by every
put). It writes `tcp-facts-<date>.review.json` beside the folder with the sha256 of the facts and of every release
asset (the zip, `HOW-TO.pdf`, `MANIFEST.json`, the `.mcpb` and its copy `tcp-facts.mcpb`, K3 of the review of 2026-10-20). `awb dataset put` refuses a folder without
that mark, with a mark of other facts or assets (a build after the review) or of another release, in one line that names the
command. So a fact a session planted with a fabricated source is read by a person before it reaches the public
dataset (G6 of the threat model). `awb kb review` is refused for the work user, and the mark and `RELEASED.jsonl`
count only when they are no link, belong to you and are writable by nobody else; the mark must name its own folder
(K2 of the review of 2026-10-20). Both are written as new files of mode 0644, never through a link.
`awb dataset put` sends the zip, the PDF and the manifest into the owner's bucket. With `--github` it also puts the
files and the build's README.md into the root of the public repository
[shimoza/tcp-facts](https://github.com/shimoza/tcp-facts), so the root README is never written by hand; LICENSE
stays as the repository has it. It commits them
as the GitHub account, tags the date and makes a release of it with the zip, the PDF, the manifest, the `.mcpb` and `tcp-facts.mcpb`. The files are
scanned for a person and a registered name first, and a hit refuses the put.
Every git call on the clone (`~/tcp-shared/datasets/tcp-facts.git`) names that path in `-c safe.directory=`, so
your put needs no global git setting. Every git and gh call sets `core.hooksPath=/dev/null`, `core.fsmonitor=false`
and `gpg.ssh.program=ssh-keygen` through `GIT_CONFIG_COUNT` (the scope of `git -c`), so no setting in the clone runs a program as you (K1 of the review of 2026-10-20). The
put refuses a clone that is not yours alone: the clone, its `.git` or anything in `.git` owned by another user, a
link or writable by group or others. Remove such a clone; the next put clones it again (git runs with umask 022).
`--github-only` does the GitHub part alone, without the bucket and without calling the key service.
Once a week this runs by itself: `awb-dataset-weekly.timer` starts `awb dataset weekly` as you every Monday 06:00
UTC (07:00 Berlin in winter), after the read tier of the fact-check on Saturday 19:00 UTC. It runs `awb dataset
build`, `awb kb review` (your mark, refused for the work user as before) and `awb dataset put --github`, each step
only after the one before succeeded, so a failed step publishes nothing after it and the put keeps all its checks.
Before that, `awb-refresh-weekly.timer` runs `awb refresh weekly` as you every Saturday 17:00 UTC, so the read
tier of 19:00 takes what the refresh requested; its failure shows as `refresh: weekly run failed: <reason>`.
A failure shows in `awb owner status` as `dataset: weekly publish failed: <step>: <reason>` until a publish succeeds;
a success is the line `dataset tcp-facts-<date>: N added, M amended, K retired` in the 08:00 mail.
The release notes say what is new since the previous release: the facts added, amended (same id, changed statement
or source) and retired, compared by id with the `facts.jsonl` of the previous release tag in the local clone, and the
requirement areas and service sections the added facts touch. The first release has no such part.
The release notes list the sha256 of the zip, the PDF and the manifest, one line each. A recipient
saves the lines as `SHA256SUMS` next to the downloads and runs `sha256sum -c SHA256SUMS`; every file must say `OK`.

### Signing a release

With `dataset_sign = yes` and `dataset_sign_key = <path of an ssh key>` in the host file, `awb dataset put --github`
signs the release: it writes `SHA256SUMS` (one line per asset: the zip, `HOW-TO.pdf`, `MANIFEST.json`, the
`.mcpb` and `tcp-facts.mcpb`) into the release folder, signs it with `ssh-keygen -Y sign -n file` into `SHA256SUMS.sig`, makes the tag with
`git tag -s` (`gpg.format ssh`, `user.signingkey` the key) and uploads both files as release assets. It signs
only the sums the review pinned: an asset that changed since refuses the put before anything is signed, and
`SHA256SUMS` is written as a new file, never through a link. Build the folder
with the same setting: the how-to, the README of the dataset and the release notes then carry "Check a download":

```bash
sha256sum -c SHA256SUMS
ssh-keygen -Y verify -f allowed_signers -I <identity> -n file -s SHA256SUMS.sig < SHA256SUMS
```

`dataset_sign_identity` names the identity (default `TCP Facts`) and `dataset_sign_allowed_signers` the file a reader
passes to `-f` (default `allowed_signers`). A put with `dataset_sign = yes` and no key is refused. Without
`dataset_sign` nothing of this runs and the block is not written.

### The dataset as an MCP server

Every build carries `tcp_facts_mcp.py`, in the zip, in the `.mcpb` and in the root of the public repository. The template is
`awb/tcp/tcp_facts_mcp.py`. It is a read-only MCP server over the files beside it: one Python file, standard library
only, Python 3.10 or newer, JSON-RPC 2.0 over stdio. It speaks the Model Context Protocol revision 2026-07-28
(`server/discover`, the version in the `_meta` of each request, no handshake) and the handshake revisions 2025-11-25
to 2024-11-05. Its tools carry a title and read-only annotations. It has four tools:

- `tcp_facts_search`: the facts ranked by word overlap, filtered by tag, grade or a minimum validation level
  (`min_level`, V0 to V4), at most 20, each with its id, level, grade, check date, source and expiry
- `tcp_service_check`: offered or not, from `services.md`, with the revision of the service description
- `tcp_price_find`: price rows of `eu-de` or `eu-nl` from the snapshot with its date; with `--live` and a service
  short name also from the public price API, no credentials needed
- `tcp_calculate`: exact decimal arithmetic with hours as the unit (a month is 720 h, as the price calculator counts it)

Every answer carries the dataset date, the best-before date and the rule "answer from these facts, cite the id, say
when no fact covers it". The server reads no file outside its dataset folder (`--data`, default the folder the file
lies in), opens no connection without `--live`, writes nothing and reads no input line longer than 8 MiB (it answers a parse error and goes on). With `--live` it reads at most 32 MiB of the price API answer and uses the snapshot when the answer is longer.

**Claude Desktop, one click.** Every build also writes `tcp-facts-<date>.mcpb` beside the zip: an MCP Bundle (a zip
with a `manifest.json`, an `icon.png`, the server file and the data files of that build, written for manifest version 0.3; the licence there is the SPDX id `CC-BY-4.0`). `awb
dataset put --github` uploads it as a release asset next to the zip, with a copy of the same bytes under the stable
name `tcp-facts.mcpb` (written into the folder by the build; the put refuses a copy that differs). So
https://github.com/shimoza/tcp-facts/releases/latest/download/tcp-facts.mcpb is always the newest bundle: the public
README names that link right under its disclaimer and again in its "MCP clients" part, and so does `HOW-TO.pdf`.
Download it and open it (double click, or drag it into Settings, Extensions). Claude Desktop shows the extension with its four tools; press
Install. It needs Python 3.10 or newer on the machine. A new build is a new file: install it over the old one. The
bundle can be checked with `npx @anthropic-ai/mcpb validate manifest.json` inside the unpacked file.

**By hand, in any client.** Unpack the zip, then add the server to the client. Replace `/path/to/tcp-facts` with the
unpacked folder.

Claude Desktop, in `claude_desktop_config.json`:

```json
{"mcpServers": {"tcp-facts": {"command": "python3", "args": ["/path/to/tcp-facts/tcp_facts_mcp.py"]}}}
```

Claude Code:

```bash
claude mcp add tcp-facts -- python3 /path/to/tcp-facts/tcp_facts_mcp.py
```

VS Code (and GitHub Copilot in it), in `.vscode/mcp.json`:

```json
{"servers": {"tcp-facts": {"type": "stdio", "command": "python3", "args": ["/path/to/tcp-facts/tcp_facts_mcp.py"]}}}
```

Add `"--live"` to the arguments for live prices. Claude Desktop opens its file under Settings, Developer, Edit Config
and loads a change only after a full quit and start. Give the path in full. On Windows the command is `python` or `py`.
In the JSON every backslash of a path is written twice.

A team that builds its own tools can take the facts alone:

```bash
awb kb export --out tcp-facts.jsonl
```

One JSON object per line:

```json
{"id": "KB-XXXX", "scope": "tcp", "statement": "...", "grade": "live", "checked": "2026-09-25",
 "expires": "2027-03-24", "expired": false, "class": "api", "tags": ["ecs"], "source": "...",
 "negative": false, "tried": []}
```

By default the export carries `live`, `contract` and `docs` facts only. Statements of the product team (`said`) may
be confidential and need a clearance first (`--grade said`). Assumptions stay out. Retired facts never leave and
expired ones only on request (`--include-expired`, marked `"expired": true`). Every entry passes the name check once
more before it is written. That check lets a registered form pass only inside the full name of a service of
`rules/services.tsv`, written as the service description writes it: a service named like a customer stays a fact
and the same word anywhere else still withholds the entry. The intake and the exit check of the push keep the plain check.

A dataset ages. Facts about availability expire after 30 days, API facts after 180. Use the `checked` and `expires`
fields. Take a fresh export rather than an old one.
