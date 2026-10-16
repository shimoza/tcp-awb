"""The old import path of the candidate rules of the intake (wipe mode, T4 of 2026-10-08).

Since F1 step 8 the rules live in the package awb/intake/wipe/, one module per family. `from awb import wipe` gives
that package itself, one module under two names: every name of a family is reached and a name set here is the one
the rules read. No module of the package imports this path, so that it never runs half way through the intake's
own import.
"""
import sys

from awb.intake import wipe as _rules

sys.modules[__name__] = _rules
