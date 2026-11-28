"""C-15 of the correctness review of 2026-11-28: `awb import --redo tcp-xxxx`, the form of the hint lines, parses
again since --redo takes a value: a project code right after a bare --redo is the CODE. The value forms stay."""
from __future__ import annotations

import argparse

from awb import importcmd


def test_redo_before_the_code_still_parses():
    assert importcmd._code_after_redo(["--redo", "tcp-q7m4"]) == ["tcp-q7m4", "--redo"]


def test_redo_with_a_number_or_a_stamp_keeps_its_value():
    assert importcmd._code_after_redo(["tcp-q7m4", "--redo", "2"]) == ["tcp-q7m4", "--redo", "2"]
    assert importcmd._code_after_redo(["tcp-q7m4", "--redo", "2026-11-27-101500"]) == \
        ["tcp-q7m4", "--redo", "2026-11-27-101500"]


def test_the_import_command_takes_the_code_after_redo(monkeypatch, capsys):
    seen = {}

    def parse(self, args=None, namespace=None):
        seen["args"] = list(args)
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_intermixed_args", parse)
    assert importcmd.main(["--redo", "tcp-q7m4"]) == 0
    assert seen["args"] == ["tcp-q7m4", "--redo"]
