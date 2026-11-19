# Suite tiers: a design note (Q-0013, step 4)

Not built. This note says what the split into a quick tier and a full tier would be, so a later task can build it
whole. Q-0013 built the part that does not need the split: the whole suite in the suite unit now runs one pytest per
test file, four at a time, with the clock at 45 minutes (the CHANGELOG line of week 47).

## Why it may still be needed

- The suite grows by one to five hundred tests a day in hardening rounds. Parallel files bought headroom, not a
  ceiling: the time per check is still the time of the whole suite.
- One check holds the runner for the whole suite. Five fixes in a row wait on each other for the full time each.

## The two tiers

1. Quick tier, per check of a branch. The files `awb build quick` already picks (UX15, `awb/buildquick.py`): the
   import-rule tests, the gate selftest, `tests/test_<module>*.py` of every changed `awb/**.py` and every test file
   that imports a changed module, plus every changed test file. It runs in the suite unit with the same isolation,
   the same per-file workers and the same hollow-test check against the baseline of those files alone. A green
   quick tier marks the check `passed: quick`, never `passed`.
2. Full tier, per deploy batch and at night. Before `sudo awb deploy --task ID` the steering session runs one full
   check of `main` plus every branch it is about to merge (one export, one suite). A nightly full run of `main`
   (a root timer next to the nightly jobs, 02:00 UTC) writes the outcome per test.

## Rules that keep it honest

- A deploy needs a full-tier pass of the exact tree it deploys. `awb deploy` refuses a commit whose newest check
  is `passed: quick`.
- A change of `tests/conftest.py`, `tests/fixtures.py`, `tests/timeouts.py`, `pyproject.toml` or of a module that
  more than a third of the test files import (`awb/config.py`, `awb/gate.py`) runs the full tier at once.
- A quick tier never skips the baseline comparison: a test of the picked files that passed on `main` and fails on
  the branch is red.

## build/tests.json from the last full run

- Today it holds about 3 800 hand-written rows against about 7 400 tests and every row says `passing`.
- The nightly full run would write `build/tests.full.json` (node, outcome, seconds) in the run folder; a command
  `awb build tests-json` turns it into `build/tests.json` with the ids kept for nodes that already have one and new
  ids for new nodes. The steering session commits it. The rule "every new test gets a row" then becomes a check
  of that command, not a hand edit.
- The seconds per file would also order the per-file queue of the suite unit (longest first), which today uses
  the file size.

## Open for the owner

- D-T1: is `passed: quick` enough to merge into `main` when the deploy batch runs the full tier later? Default:
  yes, merge on quick, deploy on full.
