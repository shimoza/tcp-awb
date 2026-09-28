# Red team of the intake and the gate of the Architect Workbench

The brief every finder of the run of 2026-09-27 worked from, with the paths of the pack in place of the scratch
folder of that run. A new finder starts here.

You are one finder of a red team. Your job: get a planted value through the intake of the Architect Workbench
into an output that a session or a reader could read, or show that a carrier of customer material is never read
at all. Every escape you find becomes a fix and a planted case. You do not fix anything yourself.

## The code under test (read it first, it is the promise you attack)

The repository is read only for you: never edit, never commit, never run pytest.

- awb/intake.py: the module text and `_Engine` (detect, sanitize, final_hits, second_hits, leaks), `_run`
- awb/matcher.py: the module text says exactly what a registered form is found as (boundaries, glued, spread)
- awb/normalize.py: what is decoded and folded before matching (entities, percent, json and rtf escapes,
  quoted printable, encoded words, invisible characters, soft hyphens, hyphenation at a line end, NFC)
- awb/patterns.py: the structured classes and their shapes (mail, url and domain, ip, mac, iban, bic, vat,
  phone, hrb, tax) and what is kept on purpose (KEEP_IP, allow-domains, platform hosts)
- awb/register.py `_variants`: the spellings the register adds for a form (umlauts long and short, genitives)
- awb/extract/__init__.py, office.py, pdf.py, text.py, mail.py, archive.py: what each reader promises to see,
  and what goes to `text` (the output), `detect_text` (detection only) and `scan_text` (names only)
- awb/planted.py and the DISGUISED list in tests/test_intake.py: the shapes already covered. Do not resubmit
  them. Mutate beyond them. The case modules of this pack are covered too: a new case is one they do not have.
- awb/gate.py (only for the gate finder): detectors and `_scan_text`

## Rules that are not negotiable

1. Invented values only. Registered forms come from tests/fixtures.py (`CUSTOMER_FORMS`, `CUSTOMER_DOMAIN`,
   `PERSON_FORMS`, `ORG_FORMS`, `LAWFIRM_FORMS`, `PLACE_FORMS`, `FILE_NUMBER`, `TENDER_ID`; `PLANTED_CANDIDATE`
   and `PLANTED_PERSON` are unregistered invented names). Structured values are invented but valid in shape:
   IBANs must pass the mod-97 check (build them, or take the ones of awb/planted.py), phone numbers use the
   area codes 030 or 040 with 555 or 1234 digits, IPv4 from 203.0.113.0/24 or 198.51.100.0/24, IPv6 from
   2001:db8::/32, domains under .example, mail addresses at .example or example.org, VAT ids DE plus nine
   digits, MAC addresses any. Never a real person, company, address, key or number. Never a name from
   anywhere else on this host. A value that is not a fixture form is declared in the module's `DERIVED` (built
   from a fixture form) or `INVENTED` (a structured value the module builds) tuple, tests/test_redteam_pack.py
   checks it. A planted secret, key, path or id is built from pieces so that the commit gate does not read the
   case file as a leak (see calibration/redteam/gate.py and tests/test_gate.py).
2. Never open, list, read, grep or copy anything under the live Workbench folders (`~/tcp-vault`, `~/tcp-shared`,
   `~/tcp-kb`, any `~/tcp-<code>` project), the owner's private folders (the password store, the agent's own
   configuration folder, the design workspace) or any folder the owner's blocklist names. Never run `awb` yourself: the harness runs the intake in a throw-away
   Workbench under your work folder. No network. No pip install. Nothing outside your work folder is written.
3. Nothing you return carries a planted value. Return ids, classes, carriers, mechanisms. The values live in
   your cases file only.
4. Cite a file by its path below its folder (awb/matcher.py:120), never with the home folder in front.

## How to work

- cases file: `calibration/redteam/<dimension>.py` (same interface as the modules there; a new dimension starts
  from the smallest one, `derived.py`)
- work folder: anywhere outside the repository, such as `/tmp/rt/<dimension>/` (the harness puts the throw-away
  Workbenches there)
- results: the `--json` file and your notes, both outside the repository

Run (intake): `.venv/bin/python calibration/redteam/harness.py calibration/redteam/<dimension>.py --work /tmp/rt/<dimension> --json /tmp/rt/<dimension>.json`
Run (gate):   `.venv/bin/python calibration/redteam/gate_harness.py calibration/redteam/gate.py --json /tmp/rt/gate.json`
Add `--only ID ...` to run a few cases. Add `--keep` to keep a throw-away Workbench and look at its outbox by
hand (`/tmp/rt/<dimension>/wb-*/tcp-shared/outbox/CUST-Q7M4/`); remove it afterwards. Add `--expected
calibration/redteam/EXPECTED.md` to see only what is new against the known leftovers.

The harness prints one line per case: caught, ESCAPE (the value is readable in an output, with the view that
read it: raw, case, skeleton, leet, reversed, decoded, word), UNSEEN (the innocent marker strings you planted next
to the value reached no output, so that carrier is never read), blocked(n) (unregistered name candidates blocked
the run; the harness then runs again with --force, and an escape that shows only then is marked forced-only).
Read the module text of calibration/redteam/harness.py for the case interface: id, cls, carrier, values, build,
visible.

Libraries you may use in a build function: the standard library (zipfile builds any office container by hand),
python-docx, openpyxl, python-pptx, reportlab, lxml, pillow. The poppler tools are installed (pdftotext, pdfinfo,
pdfimages). Where a library cannot produce a shape, write the bytes yourself.

Iterate. Write a first round of at least 12 cases across your whole dimension, run, read every line. For each
escape, mutate it to find the boundary of the gap (what smallest change makes it caught again). For each
catch, ask what nearby shape the same rule would miss. Stop when two rounds of at least 8 new cases each bring
no new escape and no new unseen carrier, or after 14 harness runs. Aim for 30 or more cases.

Judge each escape before you report it. Classes:
- promised: the value is of a class the code promises to catch, written in a shape a reader recognises, and it
  is readable in an output. This is the finding that matters.
- forced-only: readable only after --force, because unregistered name candidates blocked the plain run.
- design-limit: readable, but the code says on purpose that it does not cover this (an unregistered spelling,
  a typo, a first name alone, a class no pattern claims). Report it too, under not_promised, with what a fix
  would need.
- unseen: nothing leaked, but the carrier is never read, so a session works with material that misses it.
- artefact: the harness view is wrong (a coincidence in the skeleton view). Say why.

Write your notes file as you go: per escape the mechanism in one or two sentences (which rule in which file
misses it, and why) and a fix hint. Return the structured result at the end.
