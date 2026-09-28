# The red-team pack

The case files of the red team of 2026-09-27 (`calibration/redteam-2026-09-27.md` records the run, the fixes and
the limits kept on purpose), curated so that every case can be run again after every change of the intake or the
gate. About 1,190 cases in eight dimensions, invented values only.

| file | what it holds |
|---|---|
| `harness.py` | runs the cases of one intake dimension through a throw-away Workbench and reads every output the way a reader would |
| `gate_harness.py` | the same for the commit gate: every case written under its path and scanned |
| `structured.py` | structured data (mail, url, ip, mac, iban, bic, vat, phone, hrb, tax) in every written shape, plain text and Markdown |
| `forms.py` | written shapes of a registered form: markup, entities, encodings, look-alike letters, base64 cut at a line |
| `derived.py` | spellings a reader recognises but the register does not list: typos, first names, glued acronyms, partial ids |
| `office.py` | the hidden and half-hidden parts of docx, xlsx, pptx and the odf kinds |
| `pdf.py` | the pdf reader: spaced glyphs, annotations, fields, attachments, fonts, containers |
| `mail_archive.py` | eml and mbox, folded headers, charsets, transfer encodings; zip, tar, gzip and their corners |
| `nontext.py` | carriers that are not text: pictures, binary formats, formats without a reader, encoded blocks |
| `gate.py` | the commit gate: secrets, key pairs, tokens, private keys, home paths, blocklist words, ids, names, encodings |
| `BRIEF.md` | the rules a finder works by |
| `EXPECTED.md` | the leftovers per dimension: what still escapes or stays unseen by design |

## Running one dimension

From the repository root, with a work folder outside the repository:

```bash
.venv/bin/python calibration/redteam/harness.py calibration/redteam/structured.py --work /tmp/rt/structured --json /tmp/rt/structured.json
.venv/bin/python calibration/redteam/gate_harness.py calibration/redteam/gate.py --json /tmp/rt/gate.json
```

`--only ID ...` runs a few cases, `--skip ID ...` leaves cases out, `--keep` keeps the throw-away Workbenches for
a look by hand, `--expected calibration/redteam/EXPECTED.md` compares the run with the known leftovers: the
harness then names every new leftover (exit 1) and every listed id that is clean now, so the list can be
shortened. A case that takes ten seconds or more is marked `slow(Ns)`. A dimension takes one to two minutes on
this host; the gate runs in under a minute. The code under test is the repository that holds the
pack; `AWB_REPO` points the harness at another checkout.

The full runs are a manual step. `tests/test_redteam_pack.py` imports every module, checks the cases and runs a
small sample end to end, so the pack is proven to run without the full cost.

## What the lines mean

One line per case, then a summary. Nothing printed is ever a planted value.

- **caught**: no planted value is readable in any output of the shared side (outbox, public report, file names).
- **ESCAPE via <view>**: a value is readable in an output, in the named reader's view: `raw` as written, `case`
  folded, `skeleton` with look-alike and compatibility letters folded and separators dropped (`-notags` with
  markup removed, `-unescaped` with entities and percent escapes decoded), `leet` with digit substitutions,
  `reversed` when a bidi control is present, `decoded` inside a base64 or hex block, `word` as a whole word (for
  short values such as an acronym).
- **forced-only**: the plain run blocked on unregistered name candidates and the value is readable only in the
  second run with `--force`. That run is a real path of the owner, so it counts, but the block did its job.
- **unseen(n)**: n of the innocent marker strings planted next to the value reached no output. The carrier is
  never read, so a session works with material that misses it. Nothing leaked. Detection-only carriers
  (headers, comments, metadata, hidden sheets) are unseen by design; the module text of each reader says which.
- **clipped(k)**: k of the unseen markers are readable without their first letter. The replacement of the form
  before the marker swallowed that letter (a defect of the sanitiser, not a leak; the curation of the pack found
  it, see `EXPECTED.md`). Such a case is not an unseen carrier.
- **blocked(n)**: n unregistered name candidates stopped the plain run (the harness then runs with `--force`).
- **error**: the intake raised; the exception text is searched like an output.
- gate: **caught** when a finding of the case's class is reported for the file, **ESCAPE** otherwise, with the
  classes that were found instead (a JWT reported as a token rather than a secret is by design).

## The rules of the case modules

- Every registered form comes from `tests/fixtures.py`; a case file never spells one. Values that are not a
  fixture form are declared in the module's `DERIVED` tuple (built from a fixture form by a string operation:
  a typo, an encoding, a transliteration) or `INVENTED` tuple (a structured value the module builds: an IBAN
  that passes mod 97, a documentation address, a fake number); `tests/test_redteam_pack.py` refuses a value that
  is none of the three and checks the ranges of the invented ones.
- A planted secret, key, token, path or id is built from pieces (`"PRI" + "VATE"`, two halves of a key), the
  way `tests/test_gate.py` and the selftest of `awb/gate.py` do it, so that the commit gate prints nothing over
  the pack. `awb gate --no-register calibration/redteam/*.py calibration/redteam/*.md` must stay silent.
- No capitalised two-word literal outside the fixture words and the short list of ordinary words the test
  carries. No real name anywhere, in a value, a carrier or a comment.
- Case ids are unique within a module; `EXPECTED.md` names them under the module's heading.

## What the harness cannot see

A three letter value inside a longer word (it reads short values as whole words only), a value that straddles
two lines of a base64 or hex block (the decoded view reads the blocks line by line), a name a remapped font
shows on a rendered page (the text layer does not carry it). The finders verified such cases by hand with
`--keep`.
