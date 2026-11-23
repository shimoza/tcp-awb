"""awb lab --help names the interpreter of a lab run script (tcp-bytg ask 3, 2026-11-21): scripts that import
awb.labclient failed with plain python3 and no rule or help named the Workbench's python. `awb lab run start` said
it already; the help says it now too."""
from __future__ import annotations

import sys

from awb import cli


def test_bytg_the_lab_help_names_the_workbench_python(capsys):
    try:
        cli.main(["lab", "--help"])
    except SystemExit:
        pass
    out = " ".join(capsys.readouterr().out.split())
    assert "plain python3 has no awb module" in out and sys.executable in out
