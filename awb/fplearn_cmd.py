"""The owner's commands of the learning of the name check (awb/fplearn.py): `awb check learn` (the nightly timer
awb-check-learn.timer), `awb check allow WORD` and `awb check allow --list`. A command only: no unit imports it, so
the weekly mail reaches the key service from here and the check and the vault daemon stay free of it.
"""
from __future__ import annotations

import sys

from awb import build_units, config, fplearn


def main(argv: list[str]) -> int:
    """`awb check learn`, `awb check allow WORD`, `awb check allow --list`."""
    p = config.paths()
    if argv and argv[0] == "learn" and len(argv) == 1:
        if config.is_work_user() or config.is_build_user():
            print("awb check learn: the learning runs as the owner", file=sys.stderr)
            return 2
        try:
            out = fplearn.learn(p)
        except fplearn.LearnError as err:
            print("awb check learn: %s" % err, file=sys.stderr)
            return 2
        line = fplearn.weekly(p, out, send=build_units.send_mail)
        print(line or out.line(fplearn.iso_week(fplearn._now())))
        return 0
    if argv and argv[0] == "allow" and len(argv) == 2:
        refused = fplearn.owner_terminal()
        if refused:
            print("awb check allow: %s" % refused, file=sys.stderr)
            return 2
        if argv[1] == "--list":
            for c, s, why, t in fplearn.read_allow(p.vault):
                print("%s\t%s\t%s\t%s" % (c, s, why, t))
            return 0
        try:
            print(fplearn.allow_by_hand(p, argv[1]))
        except fplearn.LearnError as err:
            print("awb check allow: %s" % err, file=sys.stderr)
            return 2
        return 0
    print("usage: awb check learn | awb check allow WORD | awb check allow --list", file=sys.stderr)
    return 2
