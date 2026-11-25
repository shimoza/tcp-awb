"""tcp-bytg ask 2026-11-21-tcp-bytg-14 (A4), the gaps of awb winlab: extend an open window, status and log of a job
after its window ended, Out-String for a bare command, --overwrite for get and put, wait for a job, a cap on printed
output, one plain line for a missing guest credential file and start and end time in the records.

The fake machine and the project are those of tests/test_wl_session.py; the clock of the registry is put in place of
winlab._now and the pauses of wait are recorded, never slept. Every name and path is invented.
"""
from __future__ import annotations

import datetime
import json

import pytest

from awb import cli, winlab, winlabsession
from tests.test_hooks import project  # noqa: F401 (fixture)
from tests.test_wl_session import MACHINE, WINDIR, lab, logs  # noqa: F401 (fixture)


def _job(lab, capsys) -> str:
    assert cli.main(["winlab", "start", MACHINE, "--", "Start-Sleep", "5"]) == 0
    return capsys.readouterr().out.strip()


def _overwriting(lab):
    """The fake machine moves a part over an existing file when the move script says overwrite."""
    real = lab.run

    def run(text, timeout=None):
        head = json.loads(text.split("\n", 1)[0][len("# awb-winlab "):])
        if head["op"] == "file-move" and head.get("overwrite") is True:
            lab.sent.append(text)
            lab.files[head["dst"].lower()] = lab.files.pop(head["src"].lower())
            return 0, "", ""
        return real(text, timeout)

    lab.run = run


# --------------------------------------------------------------------------- extend


def test_gap_extend_gives_the_open_window_a_later_end(lab, capsys):
    before = winlab.load()[MACHINE]["window"]["end"]
    assert cli.main(["winlab", "extend", MACHINE, "--minutes", "240"]) == 0
    out = capsys.readouterr().out
    window = winlab.load()[MACHINE]["window"]
    assert "extended" in out and window["end"] > before
    span = winlab._parse(window["end"]) - winlab._parse(window["start"])
    assert span >= datetime.timedelta(minutes=239)


def test_gap_extend_keeps_the_limits_of_open_and_needs_a_window(lab, capsys):
    assert cli.main(["winlab", "extend", MACHINE, "--minutes", "1441"]) == 1
    assert "1 to 1440" in capsys.readouterr().err
    assert cli.main(["winlab", "close", MACHINE]) == 0
    capsys.readouterr()
    assert cli.main(["winlab", "extend", MACHINE, "--minutes", "30"]) == 1
    assert "no open window to extend" in capsys.readouterr().err


# --------------------------------------------------------------------------- a job after its window


def test_gap_status_and_log_work_after_the_window_ended(lab, monkeypatch, capsys):
    job = _job(lab, capsys)
    lab.files["c:\\programdata\\awb-winlab\\jobs\\%s\\log.txt" % job] = bytearray(b"row one\n")
    lab.jobs[job].update(state="ended", exit=0)
    end = winlab._parse(winlab.load()[MACHINE]["window"]["end"])
    monkeypatch.setattr(winlab, "_now", lambda: end + datetime.timedelta(minutes=10))
    assert cli.main(["winlab", "status", job]) == 0
    assert "ended with exit code 0" in capsys.readouterr().out
    assert cli.main(["winlab", "log", job]) == 0
    assert "row one" in capsys.readouterr().out
    # a new command is still refused: the window is over
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-VM"]) == 1
    assert "ended" in capsys.readouterr().err


def test_gap_a_job_of_another_window_stays_refused_after_the_end(lab, monkeypatch, capsys):
    job = _job(lab, capsys)
    path = lab.root / "notes" / "winlab" / "jobs" / ("%s.json" % job)
    record = json.loads(path.read_text())
    record["started"] = "2026-01-01T00:00:00Z"
    path.write_text(json.dumps(record))
    end = winlab._parse(winlab.load()[MACHINE]["window"]["end"])
    monkeypatch.setattr(winlab, "_now", lambda: end + datetime.timedelta(minutes=10))
    assert cli.main(["winlab", "status", job]) == 1
    assert "ended" in capsys.readouterr().err


# --------------------------------------------------------------------------- Out-String


def test_gap_a_bare_command_gets_out_string(lab, capsys):
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-VM"]) == 0
    assert "Get-VM | Out-String -Width 200" in lab.sent[-1]


def test_gap_a_command_with_a_format_or_out_command_is_sent_as_it_is(lab, capsys):
    for command in ("Get-VM | Format-Table Name", "Get-VM | Out-File C:\\lab\\x.txt", "Get-VM | ft Name"):
        assert cli.main(["winlab", "run", MACHINE, "--", command]) == 0
        assert "Out-String" not in lab.sent[-1]


def test_gap_several_statements_go_in_a_script_block_before_out_string(lab, capsys):
    assert cli.main(["winlab", "run", MACHINE, "--", "$a = 1; Get-VM"]) == 0
    assert "& {\n$a = 1; Get-VM\n} | Out-String -Width 200" in lab.sent[-1]


# --------------------------------------------------------------------------- --overwrite


def test_gap_get_overwrites_only_with_the_flag(lab, capsys):
    lab.files[(WINDIR + "a.txt").lower()] = bytearray(b"new content\n")
    target = lab.root / "results" / "a.txt"
    target.parent.mkdir()
    target.write_bytes(b"old content\n")
    assert cli.main(["winlab", "get", WINDIR + "a.txt", "results/a.txt"]) == 1
    assert "--overwrite" in capsys.readouterr().err and target.read_bytes() == b"old content\n"
    assert cli.main(["winlab", "get", WINDIR + "a.txt", "results/a.txt", "--overwrite"]) == 0, capsys.readouterr().err
    assert target.read_bytes() == b"new content\n"
    assert not list(target.parent.glob(".winlab-*")) and not list(target.parent.glob("*.awb-part"))


def test_gap_get_overwrite_never_replaces_a_folder(lab, capsys):
    lab.files[(WINDIR + "a.txt").lower()] = bytearray(b"new content\n")
    (lab.root / "results" / "a.txt").mkdir(parents=True)
    assert cli.main(["winlab", "get", WINDIR + "a.txt", "results/a.txt", "--overwrite"]) == 1
    assert "regular file only" in capsys.readouterr().err


def test_gap_put_overwrites_only_with_the_flag(lab, capsys):
    _overwriting(lab)
    (lab.root / "winlab-out" / "b.txt").write_bytes(b"fresh bytes\n")
    lab.files[(WINDIR + "b.txt").lower()] = bytearray(b"stale bytes\n")
    assert cli.main(["winlab", "put", "winlab-out/b.txt", WINDIR + "b.txt"]) == 1
    assert "exists" in capsys.readouterr().err and lab.files[(WINDIR + "b.txt").lower()] == b"stale bytes\n"
    assert cli.main(["winlab", "put", "winlab-out/b.txt", WINDIR + "b.txt", "--overwrite"]) == 0, capsys.readouterr().err
    assert lab.files[(WINDIR + "b.txt").lower()] == b"fresh bytes\n"
    assert "-Force" in lab.sent[-1]


# --------------------------------------------------------------------------- wait


def test_gap_wait_backs_off_and_prints_the_end_state(lab, monkeypatch, capsys):
    job = _job(lab, capsys)
    pauses: list[float] = []

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 4:
            lab.jobs[job].update(state="ended", exit=0)

    monkeypatch.setattr(winlabsession, "_pause", pause)
    assert cli.main(["winlab", "wait", job]) == 0
    assert "ended with exit code 0" in capsys.readouterr().out
    assert pauses == [2.0, 4.0, 8.0, 16.0]
    assert logs(lab.root).count("status of job") == 0       # one poll per pause did not fill the log


def test_gap_wait_gives_up_at_the_timeout_and_the_job_goes_on(lab, monkeypatch, capsys):
    job = _job(lab, capsys)
    pauses: list[float] = []
    monkeypatch.setattr(winlabsession, "_pause", pauses.append)
    assert cli.main(["winlab", "wait", job, "--timeout", "10"]) == winlabsession.EXIT_OFFLINE
    out, err = capsys.readouterr()
    assert "running" in out and "goes on" in err
    assert sum(pauses) == 10


def test_gap_wait_returns_3_for_a_job_that_failed(lab, monkeypatch, capsys):
    job = _job(lab, capsys)
    lab.jobs[job].update(state="ended", exit=2)
    assert cli.main(["winlab", "wait", job]) == winlabsession.EXIT_REMOTE
    assert "ended with exit code 2" in capsys.readouterr().out


# --------------------------------------------------------------------------- the output cap


def test_gap_output_over_2000_lines_is_cut_to_the_first_and_last_1000(lab, capsys):
    lab.reply = (0, "".join("row %d\r\n" % n for n in range(1, 2501)), "")
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-Rows", "|", "Out-String"]) == 0
    printed = capsys.readouterr().out.split("\n")
    assert printed[0] == "row 1" and "row 1000" in printed and "row 1001" not in printed
    assert "row 1501" in printed and printed[-2] == "row 2500"
    cut = [line for line in printed if "lines cut" in line]
    assert len(cut) == 1 and cut[0].startswith("... 500 lines cut")
    text = logs(lab.root)
    assert "row 1200" in text and "row 2500" in text            # the record keeps all of it


def test_gap_output_of_2000_lines_is_printed_whole(lab, capsys):
    lab.reply = (0, "".join("row %d\r\n" % n for n in range(1, 2001)), "")
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-Rows", "|", "Out-String"]) == 0
    out = capsys.readouterr().out
    assert "row 1200" in out and "cut" not in out


# --------------------------------------------------------------------------- a missing guest credential file


def test_gap_a_missing_credential_file_is_one_plain_line(lab, capsys):
    assert cli.main(["winlab", "run", MACHINE, "--vm", "vm-a", "--", "Get-Date"]) == 0
    script = lab.sent[-1]
    assert script.index("Test-Path -LiteralPath $awbFile") < script.index("Get-Acl")
    plain = "the credential file of the VM vm-a is missing on the lab machine: the owner puts it there"
    lab.reply = (1, "", "Get-Acl : Cannot find path 'C:\\x\\vm-a.txt'\r\nAt line:3 char:12\r\n%s\r\nAt line:4 char:1\r\n"
                 "    + CategoryInfo : ObjectNotFound\r\n" % plain)
    capsys.readouterr()
    assert cli.main(["winlab", "run", MACHINE, "--vm", "vm-a", "--", "Get-Date"]) == winlabsession.EXIT_REMOTE
    err = capsys.readouterr().err
    assert plain in err and "Get-Acl" not in err and "CategoryInfo" not in err


# --------------------------------------------------------------------------- start and end time


def test_gap_a_run_is_logged_with_its_start_and_end(lab, capsys):
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-VM"]) == 0
    assert "started 20" in logs(lab.root) and ", ended 20" in logs(lab.root)


def test_gap_a_job_record_carries_start_and_end(lab, capsys):
    job = _job(lab, capsys)
    path = lab.root / "notes" / "winlab" / "jobs" / ("%s.json" % job)
    assert "ended" not in json.loads(path.read_text())
    lab.jobs[job].update(state="ended", exit=0)
    assert cli.main(["winlab", "status", job]) == 0
    record = json.loads(path.read_text())
    assert record["started"].endswith("Z") and record["ended"].endswith("Z") and record["ended"] >= record["started"]


def test_gap_a_transfer_is_logged_with_its_start_and_end(lab, capsys):
    lab.files[(WINDIR + "a.txt").lower()] = bytearray(b"content\n")
    assert cli.main(["winlab", "get", WINDIR + "a.txt", "results/a.txt"]) == 0
    assert "started 20" in logs(lab.root) and ", ended 20" in logs(lab.root)
