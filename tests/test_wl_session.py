"""Task WL part 3 (B-037): commands, jobs and files of a project session on a Windows lab machine
(awb/winlabsession.py, awb winlab run|start|status|log|stop|put|get).

Fake transport only: FakeLab below holds the machine's files and jobs in memory and reads the JSON header of each
script it is sent. No network, no SSH, no PowerShell. The project is the one of tests/test_hooks.py with the fixture
register; the registry of the machines is winlab.json beside a keys.conf in tmp_path. Every name, code, path and
secret is invented; the secrets are made at run time.

WinLab runs no name check and no secret check on a command, a put or a get (the owner's word of 2026-11-25: it
only handles what he downloads from the internet). What the tests hold on to: the guards of the machine itself
(its own state, a {{secret:NAME}} reference, the whole WINPATH, no overwrite), the SHA-256 compare of a transfer
and the masking of the output that comes back.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import string
from pathlib import Path

import pytest

from awb import cli, config, winlab, winlabsession
from tests import fixtures
from tests.test_hooks import CODE, project  # noqa: F401 - the fixture

MACHINE = "lab-1"
FINGERPRINT = "SHA256:" + ("invented" * 6)[:43]
WINDIR = "C:\\lab\\"
_R = random.Random(20261105)


def _run(chars: str, n: int) -> str:
    return "".join(_R.choice(chars) for _ in range(n))


WORD = "Xq7!" + _run(string.ascii_letters + string.digits, 10)
KEY_ID = _run(string.ascii_uppercase, 10) + _run(string.digits, 10)
KEY_SECRET = _run(string.ascii_lowercase, 14) + _run(string.ascii_uppercase, 13) + _run(string.digits, 13)


class FakeLab(winlabsession.Transport):
    """A lab machine in memory. `down` makes every call raise Offline; `cut_after` lets that many chunk calls
    through and then goes down; `corrupt` makes the machine's SHA-256 another file's."""

    def __init__(self):
        self.files: dict[str, bytearray] = {}
        self.jobs: dict[str, dict] = {}
        self.sent: list[str] = []
        self.chunks: list[tuple[str, int]] = []
        self.reply = (0, "", "")
        self.down = False
        self.cut_after: int | None = None
        self.corrupt = False

    def _alive(self) -> None:
        if self.down:
            raise winlabsession.Offline("connection refused")

    def _tick(self) -> None:
        if self.cut_after is not None:
            if self.cut_after == 0:
                self.down = True
                raise winlabsession.Offline("connection reset")
            self.cut_after -= 1

    def run(self, text, timeout=None):
        self._alive()
        self.sent.append(text)
        first = text.split("\n", 1)[0]
        assert first.startswith("# awb-winlab {")
        head = json.loads(first[len("# awb-winlab "):])
        op = head["op"]
        if op == "run":
            return self.reply
        if op == "job-start":
            job = head["job"]
            self.jobs[job] = {"state": "running", "exit": None, "script": text}
            log = "C:\\ProgramData\\awb-winlab\\jobs\\%s\\log.txt" % job
            self.files.setdefault(log.lower(), bytearray())
            return 0, "started=%s\r\nlog=%s\r\n" % (job, log), ""
        if op in ("job-status", "job-stop"):
            job = self.jobs.get(head["job"])
            if job is None:
                return 0, "state=unknown\r\n", ""
            if op == "job-stop" and job["state"] == "running":
                job["state"] = "stopped"
            if job["state"] == "ended":
                return 0, "state=ended\r\nexit=%d\r\n" % job["exit"], ""
            return 0, "state=%s\r\n" % job["state"], ""
        if op == "file-move":
            assert head["dst"].lower() not in self.files
            self.files[head["dst"].lower()] = self.files.pop(head["src"].lower())
            return 0, "", ""
        if op == "file-remove":
            self.files.pop(head["path"].lower(), None)
            return 0, "", ""
        raise AssertionError("unknown op %s" % op)

    def put_chunk(self, path, offset, data):
        self._alive()
        self._tick()
        f = self.files.setdefault(path.lower(), bytearray())
        if offset == 0:
            f.clear()
        elif offset != len(f):
            raise ValueError("offset is not the size")
        f += data
        self.chunks.append(("put", offset))
        return len(f)

    def get_chunk(self, path, offset, size):
        self._alive()
        self._tick()
        self.chunks.append(("get", offset))
        return bytes(self.files[path.lower()][offset:offset + size])

    def stat(self, path, digest=False):
        self._alive()
        f = self.files.get(path.lower())
        if f is None:
            return None
        out = {"size": len(f), "mtime": "2026-11-05T10:00:00Z"}
        if digest:
            data = bytes(f)
            if self.corrupt:
                data = b"x" + data[1:]
            out["sha256"] = hashlib.sha256(data).hexdigest().upper()      # Get-FileHash prints capitals
        return out


@pytest.fixture
def lab(project, tmp_path, monkeypatch):  # noqa: F811
    """The project holds the open window of lab-1; the fake stands in for the transport; the session runs in the
    project folder."""
    conf = tmp_path / "conf" / "keys.conf"
    conf.parent.mkdir()
    conf.write_text("region = eu-de\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    winlab.add(MACHINE, 2201, FINGERPRINT, True, today="2026-11-05")
    winlab.open_(MACHINE, 60, CODE)
    fake = FakeLab()
    monkeypatch.setattr(winlabsession, "connect", lambda machine: fake)
    monkeypatch.setattr(winlabsession, "_pause", lambda seconds: None)
    monkeypatch.chdir(project)
    (project / "winlab-out").mkdir()
    fake.root = project
    return fake


def logs(root: Path) -> str:
    folder = root / "notes" / "winlab"
    return "".join(p.read_text(encoding="utf-8") for p in sorted(folder.rglob("*.log"))) if folder.is_dir() else ""


def assert_no_secret(text: str) -> None:
    for value in (WORD, KEY_ID, KEY_SECRET):
        assert value not in text


# --------------------------------------------------------------------------- what goes out


def test_wl_run_sends_the_command_and_logs_it_with_its_output(lab, capsys):
    lab.reply = (0, "name   state\r\n----   -----\r\nlab-vm running\r\n", "")
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-VM", "|", "Format-Table", "Name,", "State"]) == 0
    out, err = capsys.readouterr()
    assert "lab-vm running" in out and "exit 0 on lab-1" in err
    script = lab.sent[-1]
    assert "$ProgressPreference = 'SilentlyContinue'" in script and "Get-VM | Format-Table Name, State" in script
    text = logs(lab.root)
    assert "run on lab-1" in text and "Get-VM | Format-Table Name, State" in text and "lab-vm running" in text


def test_wl_run_returns_3_for_a_command_that_failed_on_the_machine(lab, capsys):
    lab.reply = (2, "", "it failed\r\n")
    assert cli.main(["winlab", "run", MACHINE, "--", "exit", "2"]) == winlabsession.EXIT_REMOTE
    assert "it failed" in capsys.readouterr().err and "exit 2" in logs(lab.root)


def test_wl_a_command_with_a_registered_name_is_refused(lab, capsys):
    """No name check of a command any more: it is sent as it is, a job, a VM name and a path pass the same way."""
    command = "Get-ADUser -Filter \"Company -eq '%s'\"" % fixtures.CUSTOMER_FORMS[0]
    assert cli.main(["winlab", "run", MACHINE, "--", command]) == 0
    err = capsys.readouterr().err
    assert "registered name" not in err and "nothing was sent" not in err
    assert len(lab.sent) == 1 and command in lab.sent[-1]
    assert "refused" not in logs(lab.root)
    job = winlabsession.start(MACHINE, command)["job"]
    encoded = re.search(r"FromBase64String\('([^']+)'\)", lab.jobs[job]["script"]).group(1)
    assert command in base64.b64decode(encoded).decode("utf-8")
    winlabsession.run(MACHINE, "Get-VM", vm=fixtures.CUSTOMER_FORMS[1])
    assert "$awbVm = '%s'" % fixtures.CUSTOMER_FORMS[1] in lab.sent[-1]
    winpath = WINDIR + fixtures.CUSTOMER_FORMS[1] + ".txt"
    lab.files[winpath.lower()] = bytearray(b"result\n")
    assert cli.main(["winlab", "get", winpath, "out.txt"]) == 0
    assert (lab.root / "out.txt").read_bytes() == b"result\n"


@pytest.mark.parametrize("command", [
    "password = %s" % WORD,
    "$pw = \"%s\"" % WORD,
    "$p = ConvertTo-SecureString '%s' -AsPlainText -Force" % WORD,
    "New-LocalUser -Name lab -Password '%s'" % WORD,
    "$env:AK=\"%s\"\n$env:SK=\"%s\"" % (KEY_ID, KEY_SECRET),
    "aws configure set aws_secret_access_key %s" % KEY_SECRET + "\nak = %s" % KEY_ID,
])
def test_wl_a_command_carrying_a_secret_is_refused(lab, capsys, command):
    """No secret check of a command any more: it is sent as it is."""
    assert cli.main(["winlab", "run", MACHINE, "--", command]) == 0
    err = capsys.readouterr().err
    assert "secret(s)" not in err and "nothing was sent" not in err and "exit 0 on lab-1" in err
    assert len(lab.sent) == 1 and command in lab.sent[-1]
    assert "refused:" not in logs(lab.root)


def test_wl_no_secret_reference_is_filled_and_the_machine_state_is_out_of_reach(lab, capsys):
    assert cli.main(["winlab", "run", MACHINE, "--", "$x = '{{secret:admin}}'"]) == 1
    assert "{{secret:NAME}} reference: nothing is filled in" in capsys.readouterr().err
    for command in ("Get-Content C:\\ProgramData\\awb-winlab\\vm\\lab-vm.txt",
                    "type $env:ProgramData\\ssh\\administrators_authorized_keys"):
        assert cli.main(["winlab", "run", MACHINE, "--", command]) == 1
        assert "the machine's own state" in capsys.readouterr().err
    assert cli.main(["winlab", "get", "C:\\ProgramData\\awb-winlab\\vm\\lab-vm.txt", "vm.txt"]) == 1
    assert lab.sent == [] and not (lab.root / "vm.txt").exists()


def test_wl_the_vm_password_never_passes_through_the_session(lab, capsys):
    lab.reply = (0, "lab-vm-guest\r\n", "")
    assert cli.main(["winlab", "run", MACHINE, "--vm", "lab-vm", "--", "hostname"]) == 0
    assert "lab-vm-guest" in capsys.readouterr().out
    script = lab.sent[-1]
    assert "Join-Path $env:ProgramData ('awb-winlab\\vm\\' + $awbVm + '.txt')" in script
    assert "$awbVm = 'lab-vm'" in script and "Invoke-Command -VMName $awbVm -Credential $awbCred" in script
    assert "'S-1-5-32-544', 'S-1-5-18'" in script and "AppendChar" in script
    # the script itself holds no password and no plain text conversion
    assert "AsPlainText" not in script
    assert cli.main(["winlab", "run", MACHINE, "--vm", "lab'vm", "--", "hostname"]) == 1
    assert "--vm takes the name of the VM" in capsys.readouterr().err


def test_wl_every_command_first_asks_allowed(lab, capsys):
    winlab.close(MACHINE)
    (lab.root / "winlab-out" / "a.txt").write_text("plain text\n", encoding="utf-8")
    for argv in (["run", MACHINE, "--", "Get-VM"], ["start", MACHINE, "--", "Get-VM"],
                 ["put", "winlab-out/a.txt", WINDIR + "a.txt", "--machine", MACHINE],
                 ["get", WINDIR + "a.txt", "a.txt", "--machine", MACHINE]):
        assert cli.main(["winlab"] + argv) == 1
        assert "has no open window" in capsys.readouterr().err
    assert cli.main(["winlab", "put", "winlab-out/a.txt", WINDIR + "a.txt"]) == 1
    assert "no lab machine has an open window for %s" % CODE in capsys.readouterr().err
    assert lab.sent == [] and lab.chunks == []


def test_wl_without_a_transport_nothing_is_sent():
    with pytest.raises(winlab.WinlabError, match="no transport to a lab machine yet"):
        winlabsession.connect({"name": MACHINE, "port": 2201})


# --------------------------------------------------------------------------- what comes back


PROGRESS = ('#< CLIXML\r\n<Objs' ' Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
            '<Obj S="progress" RefId="0"><TN RefId="0"><T>System.Management.Automation.PSCustomObject</T>'
            '<T>System.Object</T></TN><MS><I64 N="SourceId">1</I64><PR N="Record"><AV>Preparing modules for first '
            'use.</AV><AI>0</AI><Nil /><PI>-1</PI><PC>-1</PC><T>Completed</T><SR>-1</SR><SD> </SD></PR></MS></Obj>'
            '<S S="Error">Get-Item : no such item_x000D__x000A_</S><S S="Error">at line 1 &lt;here&gt;_x000D__x000A_'
            '</S></Objs>')


def test_wl_clixml_progress_records_are_stripped(lab, capsys):
    assert winlabsession.strip_clixml(PROGRESS) == "Get-Item : no such item\r\nat line 1 <here>\r\n"
    assert winlabsession.strip_clixml("plain\r\n") == "plain\r\n"
    assert winlabsession.strip_clixml("one\r\n" + PROGRESS[:200]) == "one\r\n"       # a record cut off at the end
    lab.reply = (1, PROGRESS.replace('<S S="Error">', '<S S="Output">', 1) + "\r\nfinished\r\n", PROGRESS)
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-Item", "C:\\none"]) == winlabsession.EXIT_REMOTE
    out, err = capsys.readouterr()
    text = logs(lab.root)
    for seen in (out, err, text):
        assert "CLIXML" not in seen and "<Objs" not in seen and "Preparing modules" not in seen
    assert "finished" in out and "no such item" in err and "no such item" in text


def test_wl_output_lands_by_the_rules_of_paste_own(lab, capsys):
    lab.reply = (0, "vpc 10.0.1.12 created\r\npassword = %s\r\n" % WORD, "")
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-Lab"]) == 0
    out = capsys.readouterr().out
    assert "vpc 10.0.1.12 created" in out and "password = [secret]" in out      # the lab shortening stays
    assert_no_secret(out + logs(lab.root))
    lab.reply = (0, "made for %s\r\n" % fixtures.CUSTOMER_FORMS[0], "")
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-Lab"]) == 0
    out, err = capsys.readouterr()
    # B-089: a registered name is printed and logged as written
    assert "made for %s" % fixtures.CUSTOMER_FORMS[0] in out and "withheld" not in err


# --------------------------------------------------------------------------- jobs


def test_wl_a_job_survives_a_dropped_transport(lab, capsys):
    assert cli.main(["winlab", "start", MACHINE, "--", "Start-Sleep", "3600"]) == 0
    job = capsys.readouterr().out.strip()
    assert winlabsession.JOB_RE.match(job)
    script = lab.jobs[job]["script"]
    assert "Register-ScheduledTask -TaskName 'awb-winlab-job-%s' -TaskPath '\\awb-winlab\\'" % job in script
    assert "-LogonType S4U" in script and "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    record = json.loads((lab.root / "notes" / "winlab" / "jobs" / ("%s.json" % job)).read_text(encoding="utf-8"))
    assert record["machine"] == MACHINE and record["project"] == CODE
    assert cli.main(["winlab", "status", job]) == 0
    assert capsys.readouterr().out.strip() == "job %s on lab-1: running" % job
    lab.down = True
    assert cli.main(["winlab", "status", job]) == winlabsession.EXIT_OFFLINE
    assert "machine offline" in capsys.readouterr().out
    lab.down = False
    assert cli.main(["winlab", "status", job]) == 0
    assert "running" in capsys.readouterr().out
    lab.jobs[job].update(state="ended", exit=0)
    assert cli.main(["winlab", "status", job]) == 0
    assert "ended with exit code 0" in capsys.readouterr().out
    text = logs(lab.root)
    assert "status of job %s" % job in text and "machine offline" in text


def test_wl_the_job_file_logs_everything_and_leaves_its_exit_code():
    text = winlabsession.job_file("wl-20261105-100000-abcd", "Get-VM")
    assert "} *>&1 | Out-File -LiteralPath $awbLog -Append -Encoding utf8" in text
    assert "Set-Content -LiteralPath (Join-Path $awbDir 'exit.txt') -Value $awbCode" in text
    assert text.index("Get-VM") < text.index("exit.txt")


def test_wl_log_fetches_the_new_part_and_stop_ends_the_job(lab, capsys):
    assert cli.main(["winlab", "start", MACHINE, "--", "Get-Lab"]) == 0
    job = capsys.readouterr().out.strip()
    log = lab.files["c:\\programdata\\awb-winlab\\jobs\\%s\\log.txt" % job]
    log += b"\xef\xbb\xbfstep 1 done\r\n" + ("password = %s\r\n" % WORD).encode()
    assert cli.main(["winlab", "log", job]) == 0
    out, err = capsys.readouterr()
    assert "step 1 done" in out and "password = [secret]" in out and "new bytes of job" in err
    log += b"step 2 done\r\n"
    assert cli.main(["winlab", "log", job]) == 0
    out = capsys.readouterr().out
    assert "step 2 done" in out and "step 1" not in out
    joblog = (lab.root / "notes" / "winlab" / "jobs" / ("%s.log" % job)).read_text(encoding="utf-8")
    assert "step 1 done" in joblog and "step 2 done" in joblog
    assert_no_secret(joblog + logs(lab.root))
    assert cli.main(["winlab", "stop", job]) == 0
    assert "stopped" in capsys.readouterr().out
    assert cli.main(["winlab", "status", "wl-20261105-100000-ffff"]) == 1
    assert "no job" in capsys.readouterr().err


def test_wl_the_removal_script_takes_the_job_tasks_too():
    remove = (winlab.SCRIPTS / "remove.ps1").read_text(encoding="ascii")
    assert "$JobTaskPath = '%s'" % winlabsession.TASK_PATH in remove
    assert "Step 'job-tasks'" in remove and "Unregister-ScheduledTask -TaskName $task.TaskName" in remove
    assert remove.index("Step 'job-tasks'") < remove.index("Step 'account-processes'")


# --------------------------------------------------------------------------- files


def test_wl_a_put_from_input_is_refused(lab, capsys):
    root = lab.root
    (root / "input").mkdir(exist_ok=True)
    (root / "input" / "req.txt").write_text("customer text\n", encoding="utf-8")
    assert cli.main(["winlab", "put", "input/req.txt", WINDIR + "req.txt"]) == 1
    assert "a file of input/ (customer material) never goes to a lab machine" in capsys.readouterr().err
    # a link in winlab-out/ to it, a hard link of it, a file of the vault and of the shared folder
    (root / "winlab-out" / "link.txt").symlink_to(root / "input" / "req.txt")
    os.link(root / "input" / "req.txt", root / "winlab-out" / "hard.txt")
    p = config.paths()
    p.shared.mkdir(parents=True, exist_ok=True)
    (p.shared / "s.txt").write_text("shared\n", encoding="utf-8")
    for file, said in (("winlab-out/link.txt", "a file of input/"), ("winlab-out/hard.txt", "a hard link"),
                       (str(p.register), "a file of the vault"), (str(p.shared / "s.txt"), "the shared folder"),
                       ("SCOPE.md", "winlab-out/ of the project only")):
        assert cli.main(["winlab", "put", file, WINDIR + "x.txt"]) == 1
        assert said in capsys.readouterr().err, file
    assert lab.sent == [] and lab.chunks == [] and lab.files == {}
    assert "refused: a file of input/" in logs(root)


def test_wl_put_passes_the_file_through_the_name_check_and_the_secret_check(lab, capsys):
    """No content check of a put any more: a file with a registered name and a file with a secret arrive whole,
    with their SHA-256."""
    out = lab.root / "winlab-out"
    named = ("for %s\n" % fixtures.CUSTOMER_FORMS[0]).encode()
    keyed = ("password = %s\n" % WORD).encode()
    (out / "named.txt").write_bytes(named)
    (out / "keyed.ps1").write_bytes(keyed)
    assert cli.main(["winlab", "put", "winlab-out/named.txt", WINDIR + "named.txt"]) == 0
    assert "sha256 matches" in capsys.readouterr().out
    assert cli.main(["winlab", "put", "winlab-out/keyed.ps1", WINDIR + "keyed.ps1"]) == 0
    assert "sha256 matches" in capsys.readouterr().out
    assert bytes(lab.files[(WINDIR + "named.txt").lower()]) == named
    assert bytes(lab.files[(WINDIR + "keyed.ps1").lower()]) == keyed
    assert not [path for path in lab.files if path.endswith(winlabsession.PART)]
    text = logs(lab.root)
    assert hashlib.sha256(named).hexdigest() in text and hashlib.sha256(keyed).hexdigest() in text


def test_wl_put_reads_a_large_file_in_pieces_and_refuses_a_compressed_one(lab, capsys, monkeypatch):
    """A put sends a file in chunks and nothing opens its bytes: a log with a name across the border of two
    chunks, a compressed file and a binary file all arrive whole."""
    monkeypatch.setattr(winlabsession, "CHUNK", 64)
    out = lab.root / "winlab-out"
    named = b"\x00\x01" * 28 + ("for %s\nlab row\n" % fixtures.CUSTOMER_FORMS[0]).encode()
    packed = b"\x1f\x8b\x08\x00" + b"\x00" * 200
    binary = bytes(range(256)) * 2
    assert 56 + 4 < winlabsession.CHUNK < 56 + 4 + len(fixtures.CUSTOMER_FORMS[0])
    (out / "big.log").write_bytes(named)
    (out / "big.gz").write_bytes(packed)
    (out / "big.bin").write_bytes(binary)
    for file, data in (("big.log", named), ("big.gz", packed), ("big.bin", binary)):
        lab.chunks.clear()
        assert cli.main(["winlab", "put", "winlab-out/" + file, WINDIR + file]) == 0, capsys.readouterr().err
        capsys.readouterr()
        assert bytes(lab.files[(WINDIR + file).lower()]) == data
        assert len(lab.chunks) == -(-len(data) // 64)
    assert not [path for path in lab.files if path.endswith(winlabsession.PART)]


def test_wl_get_deletes_a_copy_that_carries_a_registered_name(lab, capsys):
    """No name check of a get any more: the copy is published after its SHA-256 and nothing is deleted."""
    data = ("for %s\n" % fixtures.CUSTOMER_FORMS[0]).encode()
    lab.files[(WINDIR + "named.txt").lower()] = bytearray(data)
    assert cli.main(["winlab", "get", WINDIR + "named.txt", "results/named.txt"]) == 0
    err = capsys.readouterr().err
    assert "deleted" not in err and "registered name" not in err
    assert (lab.root / "results" / "named.txt").read_bytes() == data
    assert sorted(p.name for p in (lab.root / "results").iterdir()) == ["named.txt"]
    assert hashlib.sha256(data).hexdigest() in logs(lab.root)


def test_wl_a_put_cut_in_the_middle_resumes_and_the_hash_matches(lab, capsys, monkeypatch):
    monkeypatch.setattr(winlabsession, "CHUNK", 1000)
    data = bytes(_R.randrange(256) for _ in range(10500))
    (lab.root / "winlab-out" / "disk.bin").write_bytes(data)
    winpath = WINDIR + "disk.bin"
    lab.cut_after = 4
    assert cli.main(["winlab", "put", "winlab-out/disk.bin", winpath]) == winlabsession.EXIT_OFFLINE
    err = capsys.readouterr().err
    assert "the put broke at" in err and "the same command again resumes it" in err
    assert len(lab.files[(winpath + ".awb-part").lower()]) == 4000 and winpath.lower() not in lab.files
    lab.down, lab.cut_after, lab.chunks = False, None, []
    assert cli.main(["winlab", "put", "winlab-out/disk.bin", winpath]) == 0
    out = capsys.readouterr().out
    assert "sha256 matches, resumed at" in out
    assert lab.chunks[0] == ("put", 4000) and len(lab.chunks) == 7
    assert bytes(lab.files[winpath.lower()]) == data and (winpath + ".awb-part").lower() not in lab.files
    assert not list((lab.root / "notes" / "winlab" / "transfers").iterdir())
    text = logs(lab.root)
    assert "broken:" in text and hashlib.sha256(data).hexdigest() in text and "resumed at" in text


def test_wl_a_break_in_the_middle_is_taken_again_after_a_pause(lab, capsys, monkeypatch):
    monkeypatch.setattr(winlabsession, "CHUNK", 1000)
    data = b"line of text\n" * 400
    (lab.root / "winlab-out" / "t.txt").write_bytes(data)
    pauses = []
    monkeypatch.setattr(winlabsession, "_pause", pauses.append)
    real = lab.put_chunk

    def flaky(path, offset, chunk):
        if offset == 2000 and not pauses:
            raise winlabsession.Offline("connection reset")
        return real(path, offset, chunk)

    monkeypatch.setattr(lab, "put_chunk", flaky)
    assert cli.main(["winlab", "put", "winlab-out/t.txt", WINDIR + "t.txt"]) == 0
    assert pauses == [2] and bytes(lab.files[(WINDIR + "t.txt").lower()]) == data


def test_wl_a_wrong_hash_fails(lab, capsys, monkeypatch):
    monkeypatch.setattr(winlabsession, "CHUNK", 1000)
    data = b"lab script line\n" * 300
    (lab.root / "winlab-out" / "s.ps1").write_bytes(data)
    lab.corrupt = True
    assert cli.main(["winlab", "put", "winlab-out/s.ps1", WINDIR + "s.ps1"]) == 1
    assert "does not match the file: the part was deleted" in capsys.readouterr().err
    assert lab.files == {}
    assert "failed: the SHA-256" in logs(lab.root)
    # the same for a get: nothing lands in the project
    lab.files[(WINDIR + "r.txt").lower()] = bytearray(b"result\n" * 500)
    assert cli.main(["winlab", "get", WINDIR + "r.txt", "results/r.txt"]) == 1
    assert "does not match" in capsys.readouterr().err
    assert not (lab.root / "results" / "r.txt").exists() and not (lab.root / "results" / "r.txt.awb-part").exists()


def test_wl_a_get_cut_in_the_middle_resumes_into_the_project(lab, capsys, monkeypatch):
    monkeypatch.setattr(winlabsession, "CHUNK", 1000)
    data = b"export row\n" * 900
    lab.files[(WINDIR + "export.txt").lower()] = bytearray(data)
    lab.cut_after = 3
    assert cli.main(["winlab", "get", WINDIR + "export.txt", "results/export.txt"]) == winlabsession.EXIT_OFFLINE
    capsys.readouterr()
    assert (lab.root / "results" / "export.txt.awb-part").stat().st_size == 3000
    lab.down, lab.cut_after, lab.chunks = False, None, []
    assert cli.main(["winlab", "get", WINDIR + "export.txt", "results/export.txt"]) == 0
    assert "resumed at" in capsys.readouterr().out and lab.chunks[0] == ("get", 3000)
    assert (lab.root / "results" / "export.txt").read_bytes() == data
    # never over a file, never into input/ or out of the project
    assert cli.main(["winlab", "get", WINDIR + "export.txt", "results/export.txt"]) == 1
    assert "get does not overwrite" in capsys.readouterr().err
    for file in ("input/export.txt", "../export.txt"):
        assert cli.main(["winlab", "get", WINDIR + "export.txt", file]) == 1
        capsys.readouterr()
    assert not (lab.root / "input" / "export.txt").exists() and not (lab.root.parent / "export.txt").exists()


def test_wl_put_does_not_overwrite_a_file_on_the_machine(lab, capsys):
    (lab.root / "winlab-out" / "a.txt").write_text("plain text\n", encoding="utf-8")
    lab.files[(WINDIR + "a.txt").lower()] = bytearray(b"there before\n")
    assert cli.main(["winlab", "put", "winlab-out/a.txt", WINDIR + "a.txt"]) == 1
    assert "put does not overwrite" in capsys.readouterr().err
    assert bytes(lab.files[(WINDIR + "a.txt").lower()]) == b"there before\n"
    for winpath in ("C:\\lab\\..\\a.txt", "lab\\a.txt", "C:\\lab\\$env:x.txt", "C:\\lab\\a.txt.awb-part"):
        assert cli.main(["winlab", "put", "winlab-out/a.txt", winpath]) == 1
        assert "WINPATH" in capsys.readouterr().err, winpath


@pytest.mark.parametrize("kind", ["vm", "job", "winpath", "machine"])
def test_wl_review_session_identifiers_reject_newlines(lab, kind):
    calls = {
        "vm": lambda: winlabsession.check_vm("lab-vm\n"),
        "job": lambda: winlabsession._job_record(lab.root, "wl-20261105-101500-a1b2\n"),
        "winpath": lambda: winlabsession.check_winpath(WINDIR + "a.txt\n"),
        "machine": lambda: winlabsession.run(MACHINE + "\n", "Get-VM"),
    }
    with pytest.raises(winlab.WinlabError) as err:
        calls[kind]()
    assert "\n" not in str(err.value)
    assert not lab.sent


@pytest.mark.parametrize("place", ["notes", "winlab", "log", "hardlog"])
def test_wl_review_logs_do_not_follow_links(lab, tmp_path, place, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_text("untouched")
    if place == "notes":
        (lab.root / "notes").symlink_to(outside)
    elif place == "winlab":
        (lab.root / "notes").mkdir()
        (lab.root / winlabsession.LOG_FOLDER).symlink_to(outside)
    else:
        path = winlabsession.log_path(lab.root, MACHINE)
        path.parent.mkdir(parents=True)
        if place == "log":
            path.symlink_to(victim)
        else:
            os.link(victim, path)
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-VM"]) == 1
    capsys.readouterr()
    assert victim.read_text() == "untouched"
    assert list(outside.iterdir()) == [victim]


@pytest.mark.parametrize("field,value", [("log_offset", -1), ("log_offset", "bad"),
                                         ("log_offset", True), ("winlog", "C:\\lab\\unrelated.txt"),
                                         ("project", "tcp-zz99"), ("machine", "lab-1\n")])
def test_wl_review_job_record_is_validated_before_contact(lab, field, value):
    job = winlabsession.start(MACHINE, "Get-VM")["job"]
    path = lab.root / winlabsession.LOG_FOLDER / "jobs" / (job + ".json")
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    lab.sent.clear()
    with pytest.raises(winlab.WinlabError):
        winlabsession.job_log(job)
    assert not lab.sent and not lab.chunks


def test_wl_review_job_record_link_is_refused(lab, tmp_path):
    job = winlabsession.start(MACHINE, "Get-VM")["job"]
    path = lab.root / winlabsession.LOG_FOLDER / "jobs" / (job + ".json")
    outside = tmp_path / "job.json"
    path.rename(outside)
    path.symlink_to(outside)
    before = outside.read_bytes()
    lab.sent.clear()
    with pytest.raises(winlab.WinlabError):
        winlabsession.status(job)
    assert not lab.sent and outside.read_bytes() == before


@pytest.mark.parametrize("operation", ["command", "output", "file"])
def test_wl_review_checker_errors_do_not_echo_values(lab, monkeypatch, capsys, operation):
    from awb import hooks
    value = "pass" + "word=" + WORD + "\n" + fixtures.CUSTOMER_FORMS[0]

    def unavailable(*args, **kwargs):
        raise hooks.Unavailable(value)

    monkeypatch.setattr(hooks, "name_hits", unavailable)
    monkeypatch.setattr(hooks, "file_hits", unavailable)
    if operation == "output":
        text, note = winlabsession.landed("plain output")
        assert text == "plain output"          # B-089: no checker is asked
        result = note
    elif operation == "command":
        # a command no longer asks the checker: it is sent and its empty output needs none
        assert winlabsession.run(MACHINE, "Get-VM")["exit"] == 0
        assert len(lab.sent) == 1
        result = ""
    else:
        # a put no longer asks the checker: the file arrives
        (lab.root / "winlab-out" / "plain.txt").write_text("plain")
        assert cli.main(["winlab", "put", "winlab-out/plain.txt", WINDIR + "plain.txt"]) == 0
        assert bytes(lab.files[(WINDIR + "plain.txt").lower()]) == b"plain"
        result = capsys.readouterr().out
    assert WORD not in result + logs(lab.root)
    fixtures.assert_no_fixture_name(result + logs(lab.root), "checker refusal")


@pytest.mark.parametrize("operation", ["run", "connect"])
def test_wl_review_transport_errors_are_not_output(lab, monkeypatch, capsys, operation):
    value = "pass" + "word=" + WORD + "\n" + fixtures.CUSTOMER_FORMS[0]

    def broken(*args, **kwargs):
        raise winlabsession.Offline(value)

    if operation == "run":
        monkeypatch.setattr(lab, "run", broken)
    else:
        monkeypatch.setattr(winlabsession, "connect", broken)
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-VM"]) == winlabsession.EXIT_OFFLINE
    output = capsys.readouterr()
    text = output.out + output.err + logs(lab.root)
    assert WORD not in text
    fixtures.assert_no_fixture_name(text, "transport refusal")


@pytest.mark.parametrize("kind", ["existing", "parent"])
def test_wl_review_target_path_is_checked_before_echo(lab, kind, capsys):
    """The path of FILE no longer goes through a name check: an existing file is still never overwritten, a new
    path (also in a new folder) gets the file."""
    name = fixtures.CUSTOMER_FORMS[0]
    file = name + ".txt" if kind == "existing" else name + "/result.txt"
    if kind == "existing":
        (lab.root / file).write_text("old")
    lab.files[(WINDIR + "a.txt").lower()] = bytearray(b"plain")
    code = cli.main(["winlab", "get", WINDIR + "a.txt", file])
    out = capsys.readouterr()
    if kind == "existing":
        assert code == 1 and "get does not overwrite" in out.err
        assert (lab.root / file).read_text() == "old"
        assert not (lab.root / (file + winlabsession.PART)).exists()
    else:
        assert code == 0, out.err
        assert (lab.root / file).read_bytes() == b"plain"
    assert not list(lab.root.rglob("*" + winlabsession.PART))


@pytest.mark.parametrize("kind", ["hardlink", "fifo", "symlink"])
def test_wl_review_get_part_is_regular_and_private(lab, tmp_path, kind, capsys, monkeypatch):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    part = lab.root / "result.txt.awb-part"
    if kind == "hardlink":
        os.link(victim, part)
    elif kind == "fifo":
        os.mkfifo(part)
        fd = os.open(part, os.O_RDWR | os.O_NONBLOCK)
    else:
        part.symlink_to(victim)
    lab.files[(WINDIR + "a.txt").lower()] = bytearray(b"plain")
    real_fdopen = os.fdopen
    opened_unsafe = []

    def fdopen(fd, *args, **kwargs):
        import stat
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            opened_unsafe.append(True)
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", fdopen)
    try:
        assert cli.main(["winlab", "get", WINDIR + "a.txt", "result.txt"]) == 1
    finally:
        if kind == "fifo":
            os.close(fd)
    capsys.readouterr()
    assert victim.read_bytes() == b"keep"
    assert not opened_unsafe
    assert not (lab.root / "result.txt").exists()


def test_wl_review_get_never_overwrites_a_concurrent_file(lab, monkeypatch, capsys):
    dst = lab.root / "result.txt"
    original = lab.get_chunk

    def raced(*args):
        dst.write_bytes(b"concurrent writer")
        return original(*args)

    monkeypatch.setattr(lab, "get_chunk", raced)
    lab.files[(WINDIR + "a.txt").lower()] = bytearray(b"plain")
    assert cli.main(["winlab", "get", WINDIR + "a.txt", "result.txt"]) == 1
    capsys.readouterr()
    assert dst.read_bytes() == b"concurrent writer"


def test_wl_review_large_get_checks_names(lab, monkeypatch, capsys):
    """A get of several chunks with a registered name inside arrives whole: no name check runs."""
    monkeypatch.setattr(winlabsession, "CHUNK", 16)
    data = ("plain\n" * 10 + fixtures.CUSTOMER_FORMS[0]).encode()
    lab.files[(WINDIR + "a.txt").lower()] = bytearray(data)
    assert cli.main(["winlab", "get", WINDIR + "a.txt", "result.txt"]) == 0, capsys.readouterr().err
    capsys.readouterr()
    assert (lab.root / "result.txt").read_bytes() == data
    assert len([c for c in lab.chunks if c[0] == "get"]) == -(-len(data) // 16)
    assert not (lab.root / "result.txt.awb-part").exists()


def test_wl_a_large_compressed_get_gets_the_whole_check_and_no_name_check_per_piece(lab, monkeypatch, capsys):
    """tcp-bytg 2026-11-17: a 1.78 GB gzip chunk of a disk image took hours of rate limited name checks. A get
    now opens no file and no piece of it: the file is published after its SHA-256 with no check request."""
    import gzip

    monkeypatch.setattr(winlabsession, "CHUNK", 64)
    data = gzip.compress(b"plain sectors\n" * 200)
    lab.files[(WINDIR + "img.vhdx.0.gz").lower()] = bytearray(data)
    from awb import hooks

    asked = []
    monkeypatch.setattr(hooks, "file_hits", lambda *a, **k: asked.append("file") or {})
    monkeypatch.setattr(hooks, "name_hits", lambda *a, **k: asked.append("name") or [])
    assert cli.main(["winlab", "get", WINDIR + "img.vhdx.0.gz", "img.vhdx.0.gz"]) == 0, capsys.readouterr().err
    assert asked == [] and lab.sent == []
    assert (lab.root / "img.vhdx.0.gz").read_bytes() == data
    assert hashlib.sha256(data).hexdigest() in logs(lab.root)


def test_wl_a_large_compressed_get_with_a_registered_name_inside_is_deleted(lab, monkeypatch, capsys):
    """A compressed file with a registered name inside is published as any other file."""
    import gzip

    monkeypatch.setattr(winlabsession, "CHUNK", 16)
    data = gzip.compress(("plain\n" * 10 + "for %s\n" % fixtures.CUSTOMER_FORMS[0]).encode())
    lab.files[(WINDIR + "named.txt.gz").lower()] = bytearray(data)
    assert cli.main(["winlab", "get", WINDIR + "named.txt.gz", "named.txt.gz"]) == 0
    err = capsys.readouterr().err
    assert "deleted" not in err and "registered name" not in err
    assert (lab.root / "named.txt.gz").read_bytes() == data


def _image_with_a_key() -> bytearray:
    head = b"vhdxfile" + b"\x00" * 504
    return bytearray(head + ("\nak = %s\nsk = %s\n" % (KEY_ID, KEY_SECRET)).encode() + b"\x00" * 512)


def test_wl_a_get_of_a_disk_image_runs_the_secret_check_in_a_plain_window(lab, capsys):
    """tcp-bytg 2026-11-17, changed 2026-11-25: no secret check of a disk image any more, in any window: it arrives
    with its SHA-256 and nothing is deleted."""
    image = _image_with_a_key()
    lab.files[(WINDIR + "r23.vhdx").lower()] = image
    assert cli.main(["winlab", "get", WINDIR + "r23.vhdx", "r23.vhdx"]) == 0, capsys.readouterr().err
    assert "secret(s)" not in capsys.readouterr().err
    assert (lab.root / "r23.vhdx").read_bytes() == bytes(image)
    assert hashlib.sha256(bytes(image)).hexdigest() in logs(lab.root)


def test_wl_a_public_image_window_of_the_owner_skips_the_secret_check_of_an_image_get(lab, capsys):
    """tcp-bytg 2026-11-17, changed 2026-11-25: awb winlab open has no --public-image any more and a get checks no
    content: an image with a key and a compressed file with a registered name arrive whole; the log says
    nothing of a skipped check."""
    import gzip

    winlab.close(MACHINE)
    assert "public image" not in winlab.open_(MACHINE, 60, CODE)
    image = _image_with_a_key()
    lab.files[(WINDIR + "r23.vhdx").lower()] = image
    assert cli.main(["winlab", "get", WINDIR + "r23.vhdx", "r23.vhdx"]) == 0, capsys.readouterr().err
    assert (lab.root / "r23.vhdx").read_bytes() == bytes(image)
    assert "skipped" not in logs(lab.root)
    named = gzip.compress(("plain\n" * 10 + "for %s\n" % fixtures.CUSTOMER_FORMS[0]).encode())
    lab.files[(WINDIR + "named.txt.gz").lower()] = bytearray(named)
    assert cli.main(["winlab", "get", WINDIR + "named.txt.gz", "named.txt.gz"]) == 0
    err = capsys.readouterr().err
    assert "deleted" not in err and "registered name" not in err
    assert (lab.root / "named.txt.gz").read_bytes() == named


def test_wl_a_public_image_window_keeps_the_secret_check_of_a_plain_file(lab, capsys):
    """changed 2026-11-25: a plain file with keys is not checked either: it arrives with its SHA-256."""
    winlab.close(MACHINE)
    winlab.open_(MACHINE, 60, CODE)
    data = ("ak = %s\nsk = %s\n" % (KEY_ID, KEY_SECRET)).encode()
    lab.files[(WINDIR + "keys.txt").lower()] = bytearray(data)
    assert cli.main(["winlab", "get", WINDIR + "keys.txt", "keys.txt"]) == 0, capsys.readouterr().err
    assert "secret(s)" not in capsys.readouterr().err
    assert (lab.root / "keys.txt").read_bytes() == data
    assert not (lab.root / "keys.txt.awb-part").exists()


def test_wl_the_broker_check_carries_the_public_image_statement_and_nothing_else_passes_for_it(lab):
    """changed 2026-11-25: the answer's public_image field is ignored: a window with it, without it or with any
    value of it is returned the same; a stored window with it is as valid as one without."""
    machine = {"name": MACHINE, "end": "2026-11-17T18:00:00Z"}
    assert winlabsession._broker_machine({"machine": machine}, MACHINE) == machine
    for value in (True, False, "yes", 1):
        found = winlabsession._broker_machine({"machine": dict(machine, public_image=value)}, MACHINE)
        assert found["name"] == MACHINE and found["end"] == machine["end"]
    window = {"project": CODE, "start": "2026-11-17T10:00:00Z", "end": "2026-11-17T18:00:00Z"}
    assert winlab._valid_window(window)
    for value in (True, "yes"):
        assert winlab._valid_window(dict(window, public_image=value))


def test_wl_review_put_checks_the_bytes_it_sends(lab, monkeypatch, capsys):
    """The bytes sent are the bytes of the copy the SHA-256 was made of, also when the source changes after it."""
    src = lab.root / "winlab-out" / "a.txt"
    src.write_bytes(b"plain")
    original = winlabsession._sha256
    upload = lab.put_chunk
    sent = bytearray()

    def captured(path, offset, data):
        sent.extend(data)
        return upload(path, offset, data)

    monkeypatch.setattr(lab, "put_chunk", captured)

    def changed(path):
        result = original(path)
        src.write_text("pass" + "word=" + WORD)
        return result

    monkeypatch.setattr(winlabsession, "_sha256", changed)
    assert cli.main(["winlab", "put", "winlab-out/a.txt", WINDIR + "a.txt"]) == 0
    capsys.readouterr()
    assert sent == b"plain" and bytes(lab.files[(WINDIR + "a.txt").lower()]) == b"plain"


def test_wl_review_forged_transfer_record_does_not_skip_check(lab, capsys):
    """A transfer record only helps a resume: a forged one (another SHA-256, a `checked` field) changes nothing
    about what is sent or what the log says: the file arrives whole with the SHA-256 of its own bytes."""
    src = lab.root / "winlab-out" / "a.txt"
    data = ("pass" + "word=" + WORD).encode()
    src.write_bytes(data)
    st = src.stat()
    sha = hashlib.sha256(data).hexdigest()
    forged = hashlib.sha256(b"other bytes").hexdigest()
    record = winlabsession._record_path(lab.root, "put", MACHINE, WINDIR + "a.txt")
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"source": "winlab-out/a.txt", "ident": [st.st_size, st.st_mtime_ns, st.st_ino],
                                  "winpath": WINDIR + "a.txt", "sha256": forged, "checked": forged}))
    assert cli.main(["winlab", "put", "winlab-out/a.txt", WINDIR + "a.txt"]) == 0, capsys.readouterr().err
    capsys.readouterr()
    assert lab.chunks
    assert bytes(lab.files[(WINDIR + "a.txt").lower()]) == data
    text = logs(lab.root)
    assert sha in text and forged not in text


def test_wl_review_receive_refuses_oversized_chunk(lab, monkeypatch):
    import io
    monkeypatch.setattr(lab, "get_chunk", lambda *args: b"too many bytes")
    fh = io.BytesIO()
    with pytest.raises(winlab.WinlabError):
        winlabsession._receive(lab, fh, WINDIR + "a.txt", 0, 1)
    assert fh.getvalue() == b""


@pytest.mark.parametrize("length", [-1, True, "2", 65 * 1024 * 1024])
def test_wl_review_broker_payload_length_is_bounded(monkeypatch, length):
    import stat
    from types import SimpleNamespace
    answer = json.dumps({"ok": True, "length": length}).encode() + b"\n"

    class Socket:
        def settimeout(self, seconds): pass
        def connect(self, path): pass
        def sendall(self, data): pass
        def close(self): pass
        def recv(self, size):
            nonlocal answer
            if not answer:
                pytest.fail("an invalid length must be refused before another read")
            data, answer = answer, b""
            return data

    real_stat = os.stat
    socket_path = os.environ[winlabsession.BROKER_SOCKET_ENV]
    monkeypatch.setattr(winlabsession.os, "stat", lambda path, *args, **kwargs:
                        SimpleNamespace(st_mode=stat.S_IFSOCK) if path == socket_path
                        else real_stat(path, *args, **kwargs))
    monkeypatch.setattr(winlabsession.socket, "socket", lambda *args: Socket())
    with pytest.raises(winlabsession.Offline):
        winlabsession._broker_call({"op": "get_chunk", "size": 2})


def test_wl_review_broker_error_does_not_echo_values(monkeypatch):
    import stat
    from types import SimpleNamespace
    value = "pass" + "word=" + WORD + "\n" + fixtures.CUSTOMER_FORMS[0]

    class Socket:
        def settimeout(self, seconds): pass
        def connect(self, path): pass
        def sendall(self, data): pass
        def close(self): pass
        def recv(self, size):
            return json.dumps({"ok": False, "error": value}).encode() + b"\n"

    real_stat = os.stat
    socket_path = os.environ[winlabsession.BROKER_SOCKET_ENV]
    monkeypatch.setattr(winlabsession.os, "stat", lambda path, *args, **kwargs:
                        SimpleNamespace(st_mode=stat.S_IFSOCK) if path == socket_path
                        else real_stat(path, *args, **kwargs))
    monkeypatch.setattr(winlabsession.socket, "socket", lambda *args: Socket())
    with pytest.raises(winlab.WinlabError) as err:
        winlabsession._broker_call({"op": "check"})
    assert WORD not in str(err.value)
    fixtures.assert_no_fixture_name(str(err.value), "broker refusal")


def test_wl_review_get_pins_destination_directory(lab, tmp_path, monkeypatch, capsys):
    folder = lab.root / "results"
    folder.mkdir()
    moved = lab.root / "saved-results"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = lab.get_chunk

    def swapped(*args):
        folder.rename(moved)
        folder.symlink_to(outside)
        (outside / "a.txt.awb-part").write_bytes(b"plain")
        return original(*args)

    monkeypatch.setattr(lab, "get_chunk", swapped)
    lab.files[(WINDIR + "a.txt").lower()] = bytearray(b"plain")
    assert cli.main(["winlab", "get", WINDIR + "a.txt", "results/a.txt"]) in (0, 1)
    capsys.readouterr()
    assert list(outside.iterdir()) == [outside / "a.txt.awb-part"]
    assert (outside / "a.txt.awb-part").read_bytes() == b"plain"
    assert not (moved / "a.txt").exists() or (moved / "a.txt").read_bytes() == b"plain"


def test_wl_review_concurrent_job_logs_read_each_byte_once(lab, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    job = winlabsession.start(MACHINE, "Get-VM")["job"]
    path = "C:\\ProgramData\\awb-winlab\\jobs\\%s\\log.txt" % job
    lab.files[path.lower()] = bytearray(b"one line\n")
    barrier = threading.Barrier(2)
    original = lab.stat

    def overlapping(*args, **kwargs):
        try:
            barrier.wait(timeout=0.3)
        except threading.BrokenBarrierError:
            pass
        return original(*args, **kwargs)

    monkeypatch.setattr(lab, "stat", overlapping)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: winlabsession.job_log(job, lab.root), range(2), timeout=60))
    assert sum(result["bytes"] for result in results) == len(b"one line\n")


def test_wl_review_missing_source_is_a_safe_refusal(lab, capsys):
    value = "pass" + "word=" + WORD
    assert cli.main(["winlab", "put", "winlab-out/" + value, WINDIR + "a.txt"]) == 1
    out = capsys.readouterr()
    assert WORD not in out.out + out.err + logs(lab.root)
    assert not lab.sent and not lab.chunks


def test_wl_review_downloaded_secret_is_deleted(lab, capsys):
    """No secret check of a get any more: the file arrives with its SHA-256, nothing is deleted. The command line
    and the log still never echo its content."""
    data = ("pass" + "word=" + WORD).encode()
    lab.files[(WINDIR + "a.txt").lower()] = bytearray(data)
    assert cli.main(["winlab", "get", WINDIR + "a.txt", "result.txt"]) == 0
    out = capsys.readouterr()
    assert WORD not in out.out + out.err + logs(lab.root)
    assert (lab.root / "result.txt").read_bytes() == data
    assert not (lab.root / "result.txt.awb-part").exists()
    assert hashlib.sha256(data).hexdigest() in logs(lab.root)


def test_wl_review_oversized_output_is_withheld_before_truncation(lab, monkeypatch):
    from awb import paste
    monkeypatch.setattr(paste, "OWN_MAX_BYTES", 20)
    text = "plain line\n" * 3 + fixtures.CUSTOMER_FORMS[0]
    clean, note = winlabsession.landed(text)
    assert clean is None and "withheld" in note


@pytest.mark.parametrize("op", ["windows", "check", "run"])
def test_wl_review_broker_metadata_is_not_echoed(lab, monkeypatch, op):
    value = "pass" + "word=" + WORD
    monkeypatch.setattr(winlabsession, "_local_registry", lambda: False)
    answer = {"windows": {"machines": [value]}, "check": {"machine": {"name": MACHINE, "end": value}},
              "run": {"exit": value, "stdout": "", "stderr": ""}}[op]
    monkeypatch.setattr(winlabsession, "_broker_call", lambda *a, **kw: (answer, b""))
    with pytest.raises(winlab.WinlabError) as err:
        if op == "run":
            winlabsession.BrokerTransport(MACHINE, CODE, None).run("Get-VM")
        else:
            if op == "check":
                original = winlabsession._broker_call
                monkeypatch.setattr(winlabsession, "_broker_call", lambda req: (
                    ({"machines": [MACHINE]}, b"") if req["op"] == "windows" else original(req)))
            winlabsession.session_list(lab.root)
    assert WORD not in str(err.value)


@pytest.mark.parametrize("device", ["CON", "nul.txt", "COM1.bin", "lpt9"])
def test_wl_review_windows_device_paths_are_refused(lab, device):
    with pytest.raises(winlab.WinlabError):
        winlabsession.check_winpath(WINDIR + device)


def test_wl_review_job_log_withholds_a_line_split_at_limit(lab, monkeypatch):
    monkeypatch.setattr(winlabsession, "LOG_READ", 8)
    job = winlabsession.start(MACHINE, "Get-VM")["job"]
    path = "C:\\ProgramData\\awb-winlab\\jobs\\%s\\log.txt" % job
    lab.files[path.lower()] = bytearray(("pass" + "word=" + WORD + "\nplain\n").encode())
    results = []
    for _ in range(20):
        result = winlabsession.job_log(job)
        results.append(result["text"] or "")
        if not result["more"]:
            break
    assert WORD not in "".join(results) + logs(lab.root)
    assert not any(WORD[i:i + 6] in "".join(results) for i in range(len(WORD) - 5))


def test_wl_review_transfer_cleanup_cannot_leave_project(lab, tmp_path, monkeypatch, capsys):
    src = lab.root / "winlab-out" / "a.txt"
    src.write_text("plain")
    outside = tmp_path / "outside-records"
    outside.mkdir()
    original = winlabsession._write_json
    victim = []

    def swapped(path, record):
        original(path, record)
        if path.parent.name == "transfers":
            copy = outside / path.name
            copy.write_bytes(b"keep")
            victim.append(copy)
            path.parent.rename(path.parent.with_name("saved-transfers"))
            path.parent.symlink_to(outside)

    monkeypatch.setattr(winlabsession, "_write_json", swapped)
    cli.main(["winlab", "put", "winlab-out/a.txt", WINDIR + "a.txt"])
    capsys.readouterr()
    assert len(victim) == 1 and victim[0].read_bytes() == b"keep"
