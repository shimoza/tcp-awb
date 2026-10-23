"""K20 of the review of 2026-10-20 (task k20-extract-unit): the extract unit has the IPC, /dev/shm, proc, socket and
device isolation of the build units, the sudoers line and the probes of seal/verify.sh carry the same form, a real
pdf still extracts through the unit's argv and a unit with its socket rule opens no network socket. No root: the
real pdf runs the tool part of the argv, the socket check runs in the user's own service manager or is skipped."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from awb import build_units
from awb.extract import pdf
from tests.test_k_extract_sandbox import VERIFY, _function, _sudoers_line

K20 = ("PrivateIPC=yes", "TemporaryFileSystem=/dev/shm", "ProtectProc=invisible",
       "RestrictAddressFamilies=AF_UNIX", "PrivateDevices=yes")
LINE = "the extract unit still reads this line of a real page"


def test_k20_the_unit_carries_the_isolation_of_the_build_units_and_keeps_the_fixed_uid():
    for prop in K20:
        assert prop in pdf.UNIT_PROPERTIES, prop
    assert pdf.SHM_FS == build_units.SHM_FS.split(":")[0] and not set(":,") & set("".join(pdf.UNIT_PROPERTIES))
    assert not any(p.startswith("DynamicUser") for p in pdf.UNIT_PROPERTIES)
    argv = pdf.UnitRunner("awb-extract", b"").argv("/usr/bin/pdfinfo", ["-enc", "UTF-8"])
    assert "--uid=awb-extract" in argv and argv.index("--uid=awb-extract") < argv.index("--")


def test_k20_the_sudoers_line_is_the_argv_of_the_unit_runner(tmp_path):
    cmds = [c.strip() for c in _sudoers_line(tmp_path).split("NOPASSWD:", 1)[1].split(", ")]
    for cmd, tool in zip(cmds, ("pdftotext", "pdfinfo")):
        argv = pdf.UnitRunner("awb-extract", b"").argv("/usr/bin/%s" % tool, [])
        head = argv[2:argv.index("--")]
        head[0] = "/usr/bin/systemd-run"
        assert cmd == " ".join(head) + " -- /usr/bin/%s *" % tool


def test_k20_both_verify_probes_carry_every_property_of_the_unit():
    body = _function(VERIFY.read_text(encoding="utf-8"), "extract_checks")
    for prop in pdf.UNIT_PROPERTIES:
        assert len(re.findall(r"--property=%s(?=\s)" % re.escape(prop), body)) == 2, prop


@pytest.mark.skipif(not (shutil.which("pdftotext") and shutil.which("pdfinfo")), reason="poppler is not installed")
def test_k20_a_real_pdf_extracts_through_the_tool_part_of_the_unit_argv(tmp_path, monkeypatch):
    path = tmp_path / "real.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawString(72, 700, LINE)
    c.save()
    seen = []

    def unit(argv, input=None, stdout=None, stderr=None, timeout=None, check=False, preexec_fn=None):
        assert argv[:3] == ["sudo", "-n", "systemd-run"]
        assert ["--property=%s" % p for p in pdf.UNIT_PROPERTIES] == [a for a in argv if a.startswith("--property=")]
        tail = argv[argv.index("--") + 1:]
        seen.append(Path(tail[0]).name)
        assert str(path) not in argv
        return subprocess.run(tail, input=input, stdout=stdout, stderr=stderr, timeout=timeout, check=False)

    monkeypatch.setattr(pdf, "runner_for", lambda p: pdf.UnitRunner("awb-extract", Path(p).read_bytes(), run=unit))
    ex = pdf.extract(path)
    assert ex.state == "ok" and ex.meta["extractor"] == "unit", ex.notes
    assert LINE in ex.text
    assert {"pdftotext", "pdfinfo"} <= set(seen)


def _user_manager() -> bool:
    if not shutil.which("systemd-run") or not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:
        res = subprocess.run(["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect", "--", "/bin/true"],
                             capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0


PROBE = ("import socket\n"
         "for fam in (socket.AF_INET, socket.AF_INET6, socket.AF_PACKET, socket.AF_NETLINK):\n"
         "    try:\n"
         "        socket.socket(fam, socket.SOCK_RAW if fam == socket.AF_PACKET else socket.SOCK_DGRAM)\n"
         "        print('open', int(fam))\n"
         "    except OSError as err:\n"
         "        print('refused', int(fam), err.errno)\n"
         "socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
         "print('unix')\n")


@pytest.mark.skipif(not _user_manager(), reason="no service manager of this user to start a unit without root")
def test_k20_the_socket_rule_of_the_unit_opens_no_network_socket():
    rule = next(p for p in pdf.UNIT_PROPERTIES if p.startswith("RestrictAddressFamilies="))

    def run(props):
        argv = ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect"]
        argv += ["--property=%s" % p for p in props] + ["--", sys.executable, "-I", "-c", PROBE]
        return subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout.split("\n")

    out = run(["NoNewPrivileges=yes", rule])
    assert [ln.split()[0] for ln in out if ln.startswith(("open", "refused"))] == ["refused"] * 4, out
    assert "unix" in out
    control = run(["NoNewPrivileges=yes"])            # the probe can tell: without the rule a socket opens
    assert any(ln.startswith("open") for ln in control), control


@pytest.mark.skipif(not shutil.which("visudo"), reason="no visudo on this host")
def test_k20_visudo_parses_the_sudoers_line(tmp_path):
    f = tmp_path / "extract.sudoers"
    f.write_text(_sudoers_line(tmp_path).lstrip("#").replace("1000", "#1000", 1) + "\n", encoding="utf-8")
    res = subprocess.run(["visudo", "-cf", str(f)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr
