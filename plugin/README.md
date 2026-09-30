# The awb plugin

The Architect Workbench as a Claude Code plugin: six skills, two workers and the five checkpoints of a working
session.

| part | what |
|---|---|
| `awb:tcp-facts` | answers about TCP from checked facts (`awb kb`), with the entry of every fact |
| `awb:tcp-price` | live prices and price sheets checked to the cent (`awb price`) |
| `awb:review` | the review of a deliverable before it leaves (`awb review`, `awb write check`) |
| `awb:test-tenant` | what runs and ran on the test tenants (`awb tenant`) |
| `awb:azure-to-tcp` | Azure machines to TCP in phases, from the inventory to a reviewed plan (`awb migrate`) |
| workers | `migration-worker-files` (reads and writes files) and `migration-worker-shell` (also runs `awb`), one phase each |
| `awb:drafting` | texts in the architect's own voice; the style is the author's, replace it with yours |
| hooks | the prompt, before and after a write, the start and the end of a session (`awb hook`) |

The plugin brings the instructions and the checkpoints. The `awb` command itself must be installed on the same
machine first, see the README at the repository root.

    claude plugin marketplace add shimoza/tcp-awb
    claude plugin install awb@tcp-awb

On a sealed host the seal already installs the hooks for the work user as managed settings. Do not install the
plugin there as well, or every hook runs twice.
