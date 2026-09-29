# Architect Workbench

A toolkit for cloud solution architects who work with an AI coding assistant (Claude Code). It helps to design a
solution, price it, check it against the platform and build it on a live tenant. Every fact the assistant uses
comes from a checked source. Every price is fetched live. Every result is reviewed before it leaves.

It is built for T Cloud Public (TCP), but most of it is not tied to one cloud.

## What it does for an architect

**It remembers the platform so you do not have to.** You no longer keep in your head which endpoint a service has
in which region or how a call is built and signed. The Workbench also knows which project id goes into the path,
how a list is paged and which call behaves differently from the documentation. Its knowledge base keeps these
details as checked API facts. Each one was proven with a live call on a tenant and is rechecked every 180 days:
the calls that work with their parameters, the ones that do not and why, what differs between regions. The API
client builds the endpoint from the service and the region and signs the request. It finds the project id itself,
pages through long lists and tells an empty answer from one it could not read. The architect thinks about the
solution. The Workbench knows where everything is and how it works.

**Prices, always live.** `awb price find` reads the public price API of the platform, `awb price snapshot` keeps a
listing per region. `awb price diff` shows what changed since the last one. `awb price check` takes a price
sheet and recomputes it to the cent: every price fetched again, every total and the grand total recomputed. A price
is never taken from memory.

**Knowledge you can trust.** `awb kb` holds checked facts about the platform. Each entry is one fact with its
source, its grade and the date it was checked:

| grade | meaning |
|---|---|
| `live` | checked on the platform itself |
| `contract` | from the service description |
| `docs` | from the documentation |
| `said` | a statement of the product team |
| `assumed` | not checked yet, used as a lead only |

A negative ("this is not possible") needs two recorded attempts. Facts that go stale (availability, flavors,
regions) carry an expiry date. `awb refresh` brings the documentation mirrors up to date, compares prices with the
last snapshot and rechecks what expired. New facts only get in through the checks of `awb kb add`.

**Architectures checked before they leave.** `awb review` turns a deliverable (a target architecture, a migration
concept or a customer answer) into a list of claims, each with its evidence. For an important text a
review board reads it through several lenses: an architect of the source platform, an architect of the target
platform and the view of the partner who has to sell and support it, plus a fidelity pass that looks for invented
facts, contradictions and a misread question. The findings are applied by rewriting the answer, not by patching it.

**A live solution on a live tenant.** A session builds and checks a lab setup on a real TCP tenant through a signed
API client (`awb cloud`). Jobs keep to a time budget, record every finished step and resume where they stopped.
`awb cloud sweep` finds servers, disks and addresses nobody tagged or used for a while.

**Texts that read like the architect wrote them.** `awb write check` measures a text against the architect's own
style (sentence length, the connectors he uses, typical AI words) and blocks what does not fit. A drafting skill
writes mails, offers and PoC documents in that voice and proves that the voice pass changed no fact.

**A folder per project.** `awb spawn` creates a project with its scope, rules, hooks and git. Sessions pick
up where the last one stopped. `awb ledger` keeps a line per activity for the weekly report.

## Customer data

In this concept version the Workbench runs on a public model. **No customer data goes into a session.** Work with
invented or public material only. Describe no customer in a way that makes it recognisable (a region, an
industry and a headquarters together can name a company as clearly as its name).

The Workbench is built to keep it that way and to move to a private model later:

- `awb intake` turns documents into working copies with a code (`CUST-XXXX`) in place of every registered name. It
  also takes out bank data, tax ids, phone numbers, addresses, secrets and hidden text. The register of names stays on
  the owner's side. It is tested with 1,188 invented cases (`calibration/redteam/`).
- The sealed setup runs every session as a separate system user that cannot read the register at all.
- Five hooks check every prompt, every written file and the start and end of every session, so that a registered
  name typed by mistake is stopped before the model sees it.
- A gate checks every commit for names, secrets, home paths and the owner's blocklist. A send gate lets only the
  exact reviewed version of a deliverable leave.

These checks are a safeguard, not a guarantee. The architect stays responsible for what goes into a session.

## What you need

- A Linux host. Tested on Ubuntu 24.04 with Python 3.12 on ext4.
- `git` and `gpg`.
- The poppler tools: `pdftotext`, `pdfinfo`, `pdfimages`, `pdfdetach`.
- Claude Code with your own subscription login. The `awb` command itself needs no model and no API key.
- For the sealed setup: `sudo` on the host.

Runtime code is the Python standard library plus the poppler tools. The tests use `pytest`, `python-docx`,
`openpyxl` and `reportlab`.

## Try it

```bash
git clone https://github.com/shimoza/tcp-awb.git ~/tcp-awb
cd ~/tcp-awb
python3 -m venv .venv
.venv/bin/pip install -e . pytest python-docx openpyxl reportlab
.venv/bin/pytest
.venv/bin/awb gate --selftest
```

The tests use invented names only (`tests/fixtures.py`) and a throw-away Workbench each. They never touch your own
folders.

## As a plugin for Claude Code

The skills and the checkpoints also come as a plugin (`plugin/`), with this repository as its marketplace. Install
`awb` first (see above), then:

```bash
claude plugin marketplace add shimoza/tcp-awb
claude plugin install awb@tcp-awb
```

It adds `/awb:tcp-facts`, `/awb:tcp-price`, `/awb:review`, `/awb:test-tenant` and `/awb:drafting`. It also adds the five
hooks. `plugin/README.md` has the details.

## The sealed setup

The seal uses two users on one host. You (the owner, with `sudo`) keep the register and run the intake. The work user `awb` (no
`sudo`) runs every Claude Code session. A small daemon answers the sessions' name checks with positions and classes
only. `seal/README.md` explains the design and its limits.

```bash
sudo seal/setup.sh --dry-run     # prints every step, changes nothing
sudo seal/setup.sh               # add --mirrors DIR... for read-only doc mirrors
# log out and in again: you are now in the group awb
sudo seal/verify.sh              # every line must say PASS
awb vault encrypt                # the register encrypted at rest; after a reboot: awb vault unlock
ssh awb@<host>                   # the work user takes your ssh keys
awb seal check                   # as awb: what the work user can and cannot reach
```

Then connect Claude Code to the host as `awb` and test the hooks in a real session with an invented test name.
After a code change run `sudo seal/setup.sh` again, then `sudo systemctl restart awb-vaultd` and `awb vault unlock`.

Things you want no commit to carry (names of other workspaces, a naming scheme) go into a local blocklist, one
regular expression per line: `/etc/awb/blocklist.txt` or `~/.config/awb/blocklist.txt`. It never enters the
repository.

## Layout

| folder | what |
|---|---|
| `awb/` | the package and the `awb` command |
| `rules/` | one rule per file with its reason and what enforces it, plus the data files of the checks |
| `hooks/` | git hooks: `pre-commit`, `commit-msg` and `pre-push` run the gate |
| `seal/` | setup and verification of the two-user seal, the work user's client settings |
| `plugin/` | the Claude Code plugin; `.claude-plugin/marketplace.json` makes this repository its marketplace |
| `workflows/` | the review and refresh workflows for Claude Code |
| `calibration/` | the review calibration set, the red-team pack and the records of the red team and the security review |
| `tests/` | every test, invented names only |
| `COMMANDS.md` | every `awb` command by who runs it |
| `INTERFACES.md` | the interface of every module |

## Personal parts

- `rules/voice.txt`, the drafting skill in `seal/work-claude/skills/drafting/` and `tests/test_his_voice.py` carry
  the author's own writing style, measured from his own typed English. Replace them with yours.
- The knowledge base of TCP facts is not part of this repository. `awb kb` works on an empty one.

## License

The Workbench is licensed under the Apache License 2.0. The text is in `LICENSE`, the copyright in `NOTICE`.
