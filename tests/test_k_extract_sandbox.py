"""The extract sandbox findings K7, K18, K19 of the review of 2026-10-20 (task k-extract-sandbox).

Temporary folders and fakes only: no unit is started and no sudoers file is written."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from awb import config
from awb.extract import pdf

REPO = Path(__file__).resolve().parent.parent
SETUP = REPO / "seal" / "setup.sh"
VERIFY = REPO / "seal" / "verify.sh"


def _function(text: str, name: str) -> str:
    return re.search(r"^%s\(\) \{\n.*?^\}\n" % name, text, re.S | re.M).group(0)


def _sudoers_line(tmp_path) -> str:
    """The sudoers text step_extract_user writes, run in bash with the host commands stubbed."""
    body = SETUP.read_text(encoding="utf-8")
    script = "\n".join([
        "set -u",
        "EXTRACT_USER=awb-extract; EXTRACT_SUDOERS=%s/sudoers; dry_run=1; owner=someone" % tmp_path,
        "info() { :; }; run() { :; }; skip() { :; }; getent() { return 0; }; id() { echo 1000; }",
        'command() { [ "$1" = -v ] && echo "/usr/bin/$2"; }',
        'write_file() { cat; }',
        _function(body, "step_extract_user"),
        _function(body, "extract_prefix"),
        "step_extract_user",
    ])
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return [ln for ln in res.stdout.splitlines() if "NOPASSWD" in ln][0]


def test_k7_every_sudoers_command_carries_the_whole_fixed_prefix(tmp_path):
    line = _sudoers_line(tmp_path)
    cmds = line.split("NOPASSWD:", 1)[1].split(", ")
    assert len(cmds) == 2
    for cmd, tool in zip(cmds, ("pdftotext", "pdfinfo")):
        cmd = cmd.strip()
        assert cmd.startswith("/usr/bin/systemd-run --quiet --wait --pipe --collect --uid=awb-extract ")
        assert "NoNewPrivileges=yes" in cmd and "RuntimeMaxSec=120" in cmd
        assert cmd.endswith(" -- /usr/bin/%s *" % tool)


def test_k7_no_sudoers_command_is_a_bare_tool(tmp_path):
    line = _sudoers_line(tmp_path)
    for cmd in line.split("NOPASSWD:", 1)[1].split(","):
        assert cmd.strip().startswith("/usr/bin/systemd-run")


def test_k7_verify_probes_pdfinfo_in_the_unit():
    body = _function(VERIFY.read_text(encoding="utf-8"), "extract_checks")
    assert 'command -v pdfinfo' in body
    assert "runs pdfinfo on a one page pdf" in body


# ---------------------------------------------------------------------------------------------- K18


def test_k18_a_unit_output_over_the_cap_fails_the_call(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_OUTPUT", 1000)
    runner = pdf.UnitRunner("extract-test", b"data")
    monkeypatch.setattr(runner, "argv", lambda tool, options, out=False: [
        "python3", "-c", "import sys; sys.stdout.write('a' * 100000)"])
    with pytest.raises(OSError):
        runner.run("pdftotext", [], "x", True)


def test_k18_an_output_under_the_cap_is_returned_whole(monkeypatch):
    runner = pdf.UnitRunner("extract-test", b"data")
    monkeypatch.setattr(runner, "argv", lambda tool, options, out=False: [
        "python3", "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"])
    code, out, err = runner.run("pdftotext", [], "x", True)
    assert (code, out, err) == (0, "DATA", "")
