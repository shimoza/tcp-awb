"""`awb build`: the self-build of the Workbench (presentations/self-build).

    awb build serve     the runner of the build queue, the ExecStart of awb-buildd.service

This release holds the host side of step SB1 only: the build user, its folder, the pinned client, the units and the
guard. The queue (SB2) and the runner (SB3) are not built yet, so `serve` refuses with exit 2 and changes nothing.
seal/setup.sh installs awb-buildd.service without enabling it until the runner exists.
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb build", description="The self-build of the Workbench (presentations/self-build).")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    sub.add_parser("serve", help="the runner of the build queue (not built yet: SB3)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.command is None:
        ap.print_usage(sys.stderr)
        return 2
    print("awb build: the build queue and its runner are not built yet (presentations/self-build SB2 and SB3); "
          "nothing was done", file=sys.stderr)
    return 2
