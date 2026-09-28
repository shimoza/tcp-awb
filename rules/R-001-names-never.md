# R-001 Names never

A real customer, partner or person name appears nowhere in the Workbench: not in a project, a report, a knowledge
entry, a file name, a folder name, a commit or a log. Codes of the one grammar (`awb/codes.py`) stand in their place.

Reason: the work is shareable and pushable at any moment only if it is clean by construction, not cleaned before
each export. Enforced by: `awb intake` (the only way in), `awb gate` on every commit, `awb check` for texts.
