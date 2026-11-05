"""Task WL part 3 (B-037): commands, jobs and files of a project session on a Windows lab machine
(awb/winlabsession.py, awb winlab run|start|status|log|stop|put|get).

Fake transport only: FakeLab below holds the machine's files and jobs in memory and reads the JSON header of each
script it is sent. No network, no SSH, no PowerShell. The project is the one of tests/test_hooks.py with the fixture
register; the registry of the machines is winlab.json beside a keys.conf in tmp_path. Every name, code, path and
secret is invented; the secrets are made at run time.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
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
    command = "Get-ADUser -Filter \"Company -eq '%s'\"" % fixtures.CUSTOMER_FORMS[0]
    assert cli.main(["winlab", "run", MACHINE, "--", command]) == 1
    err = capsys.readouterr().err
    assert "1 registered name(s): nothing was sent" in err
    assert lab.sent == []
    text = logs(lab.root)
    assert "refused: the command carries 1 registered name(s)" in text
    fixtures.assert_no_fixture_name(err + text, "refusal and log")
    # a job, a VM name and a path on the machine pass the same check
    with pytest.raises(winlab.WinlabError, match="registered name"):
        winlabsession.start(MACHINE, command)
    with pytest.raises(winlab.WinlabError, match="registered name"):
        winlabsession.run(MACHINE, "Get-VM", vm=fixtures.CUSTOMER_FORMS[1])
    with pytest.raises(winlab.WinlabError, match="registered name"):
        winlabsession.get(WINDIR + fixtures.CUSTOMER_FORMS[1] + ".txt", "out.txt")
    assert lab.sent == []


@pytest.mark.parametrize("command", [
    "password = %s" % WORD,
    "$pw = \"%s\"" % WORD,
    "$p = ConvertTo-SecureString '%s' -AsPlainText -Force" % WORD,
    "New-LocalUser -Name lab -Password '%s'" % WORD,
    "$env:AK=\"%s\"\n$env:SK=\"%s\"" % (KEY_ID, KEY_SECRET),
    "aws configure set aws_secret_access_key %s" % KEY_SECRET + "\nak = %s" % KEY_ID,
])
def test_wl_a_command_carrying_a_secret_is_refused(lab, capsys, command):
    assert cli.main(["winlab", "run", MACHINE, "--", command]) == 1
    err = capsys.readouterr().err
    assert "secret(s): no password and no key goes to a lab machine, nothing was sent" in err
    assert lab.sent == []
    assert_no_secret(err + logs(lab.root))
    assert "refused:" in logs(lab.root)


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
    # the script itself would pass the secret check: no password and no plain text conversion in it
    assert winlabsession._secret_count(script) == 0 and "AsPlainText" not in script
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
    assert "vpc 10.0.1.12 created" in out and "password = [secret]" in out
    assert_no_secret(out + logs(lab.root))
    lab.reply = (0, "made for %s\r\n" % fixtures.CUSTOMER_FORMS[0], "")
    assert cli.main(["winlab", "run", MACHINE, "--", "Get-Lab"]) == 0
    out, err = capsys.readouterr()
    assert "withheld: it carries a registered name" in err
    fixtures.assert_no_fixture_name(out + err + logs(lab.root), "withheld output")


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
    out = lab.root / "winlab-out"
    (out / "named.txt").write_text("for %s\n" % fixtures.CUSTOMER_FORMS[0], encoding="utf-8")
    (out / "keyed.ps1").write_text("password = %s\n" % WORD, encoding="utf-8")
    assert cli.main(["winlab", "put", "winlab-out/named.txt", WINDIR + "named.txt"]) == 1
    assert "registered name(s): nothing was sent" in capsys.readouterr().err
    assert cli.main(["winlab", "put", "winlab-out/keyed.ps1", WINDIR + "keyed.ps1"]) == 1
    assert "secret(s)" in capsys.readouterr().err
    assert lab.chunks == [] and lab.files == {}


def test_wl_put_reads_a_large_file_in_pieces_and_refuses_a_compressed_one(lab, capsys, monkeypatch):
    monkeypatch.setattr(winlabsession, "CHECK_WHOLE_MAX", 100)
    monkeypatch.setattr(winlabsession, "PIECE", 64)
    out = lab.root / "winlab-out"
    # the name lies across the border of two pieces
    (out / "big.log").write_bytes(b"\x00\x01" * 28 + ("for %s\nlab row\n" % fixtures.CUSTOMER_FORMS[0]).encode())
    assert 56 + 4 < winlabsession.PIECE < 56 + 4 + len(fixtures.CUSTOMER_FORMS[0])
    (out / "big.gz").write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 200)
    (out / "big.bin").write_bytes(bytes(range(256)) * 2)
    assert cli.main(["winlab", "put", "winlab-out/big.log", WINDIR + "big.log"]) == 1
    assert "registered name(s): nothing was sent" in capsys.readouterr().err
    assert cli.main(["winlab", "put", "winlab-out/big.gz", WINDIR + "big.gz"]) == 1
    assert "cannot open this compressed file" in capsys.readouterr().err
    assert lab.chunks == []
    assert cli.main(["winlab", "put", "winlab-out/big.bin", WINDIR + "big.bin"]) == 0
    assert bytes(lab.files[(WINDIR + "big.bin").lower()]) == bytes(range(256)) * 2


def test_wl_get_deletes_a_copy_that_carries_a_registered_name(lab, capsys):
    lab.files[(WINDIR + "named.txt").lower()] = bytearray(("for %s\n" % fixtures.CUSTOMER_FORMS[0]).encode())
    assert cli.main(["winlab", "get", WINDIR + "named.txt", "results/named.txt"]) == 1
    err = capsys.readouterr().err
    assert "registered name(s): the copy was deleted" in err
    assert not list((lab.root / "results").iterdir())
    fixtures.assert_no_fixture_name(err + logs(lab.root), "refusal and log")


def test_wl_a_put_cut_in_the_middle_resumes_and_the_hash_matches(lab, capsys, monkeypatch):
    monkeypatch.setattr(winlabsession, "CHUNK", 1000)
    data = bytes(_R.randrange(256) for _ in range(10500))
    (lab.root / "winlab-out" / "disk.bin").write_bytes(data)
    monkeypatch.setattr(winlabsession, "check_file", lambda path, size: None)   # the checks have their own tests
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
