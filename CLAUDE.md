# Architect Workbench

The instructions for a Claude Code session that works on this repository.

## Rules

- No real customer, partner or person name exists in this repository, in its tests, in its history or in its
  issues. Tests use the invented names of `tests/fixtures.py` only. A new invented name is added there first.
- Customer material enters a project only through `awb intake`. A session never reads an original.
- The register of names and codes lives in the vault on the owner's side. A working session never reads anything
  there. Tests use a temporary vault with invented names.
- Every text a session writes carries codes (`CUST-Q7M4`, `tcp-q7m4`) and never a name. The commit gate
  (`awb gate`) refuses names, secrets, home paths and whatever the owner's local blocklist names.
- Runtime code is standard library plus the poppler tools. Tests may use python-docx, openpyxl, reportlab.
- `build/tests.json` lists every test with its status: run the tests and continue with the first failing one. A test
  is never edited to make it pass.
- Commits carry the owner as author and nothing else: no co-author line and no "generated with" line. The
  commit-msg hook refuses a message that names an assistant as co-author.
- English. No em-dash. No comma before "and" or "or". Plain words.

## Commands

```bash
.venv/bin/pytest
.venv/bin/awb gate --selftest
```
