"""Task WL part 4 (B-038): the lab broker, the root owned connector to the owner's Windows lab laptop
(awb/tcp/winbroker.py, awb winlab serve), its client in awb/winlabsession.py and the online machines of awb owner
status.

A fake ssh only: FakeSsh below stands in for ssh-keyscan, ssh and sftp, reads the script the broker writes to the
standard input of ssh and the chunk it puts over sftp (B-039) and keeps the machine's files in memory. Nothing is
reached, nothing is run on a
machine. The registry is winlab.json beside a keys.conf in tmp_path, the project folders and their key tokens are
in tmp_path too. Every machine name, code, key and fingerprint is invented; the keys are made at run time.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from awb import config, ownerwait, projects, winlab, winlabsession
from awb.tcp import winbroker

MACHINE = "lab-1"
OTHER_MACHINE = "lab-2"
PORT = 2201
PROJECT = "tcp-ab2c"
OTHER = "tcp-zz99"
T0 = datetime.datetime(2026, 11, 5, 10, 0, tzinfo=datetime.timezone.utc)


def host_key() -> str:
    """An invented ed25519 public key blob, base64, as ssh-keyscan prints it."""
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + secrets.token_bytes(32)
    return base64.b64encode(blob).decode("ascii")


class FakeSsh:
    """ssh-keyscan answers `scan_key`; ssh fails with `ssh_error` or runs the broker's script against `files`."""

    def __init__(self, key: str):
        self.scan_key = key
        self.calls: list[list[str]] = []
        self.sent: list[str] = []
        self.known: list[str] = []
        self.files: dict[str, bytearray] = {}
        self.reply = (0, b"", b"")
        self.ssh_error: bytes | None = None

    def ssh_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "ssh"]

    def __call__(self, argv, data, timeout):
        self.calls.append(list(argv))
        if argv[0] == "ssh-keyscan":
            port = argv[argv.index("-p") + 1]
            return 0, ("[127.0.0.1]:%s ssh-ed25519 %s\n" % (port, self.scan_key)).encode("ascii"), b"# banner\n"
        if argv[0] == "sftp":
            # B-039: the bytes of a put come over sftp to PART.awb-chunk; the batch is on standard input
            local, remote = re.search(r'^put "([^"]+)" "([^"]+)"$', data.decode("utf-8"), re.M).groups()
            self.files[remote[1:].replace("/", "\\").lower()] = bytearray(Path(local).read_bytes())
            return 0, b"", b""
        assert argv[0] == "ssh"
        known = next(a.split("=", 1)[1] for a in argv if a.startswith("UserKnownHostsFile="))
        self.known.append(Path(known).read_text(encoding="ascii"))
        if self.ssh_error is not None:
            return 255, b"", self.ssh_error
        cut = data.index(b"\n")
        size = int(data[:cut])
        script = data[cut + 1:cut + 1 + size].decode("utf-8")
        body = data[cut + 1 + size:]
        self.sent.append(script)
        m = re.search(r"^\$p = '(.*)'$", script, re.M)
        path = m.group(1).replace("''", "'").lower() if m else None
        if "$r.CopyTo($f)" in script:
            offset = int(re.search(r"^\$o = \[long\](\d+)$", script, re.M).group(1))
            body = self.files.pop(path + ".awb-chunk")
            f = self.files.setdefault(path, bytearray())
            if offset == 0:
                f.clear()
            elif offset != len(f):
                return 5, b"size=%d" % len(f), b""
            f += body
            return 0, b"size=%d" % len(f), b""
        if "ToBase64String" in script:
            want = int(re.search(r"^\$want = \[int\](\d+)$", script, re.M).group(1))
            offset = int(re.search(r"Seek\(\[long\](\d+)", script).group(1))
            piece = bytes(self.files[path][offset:offset + want])
            return 0, b"data=" + base64.b64encode(piece), b""
        if "Get-FileHash" in script:
            f = self.files.get(path)
            if f is None:
                return 0, b"none=1", b""
            out = "size=%d\nmtime=2026-11-05T10:00:00.0000000Z" % len(f)
            if "if ($true)" in script:
                out += "\nsha256=" + hashlib.sha256(bytes(f)).hexdigest().upper()
            return 0, out.encode("ascii"), b""
        return self.reply


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """The owner registered lab-1 with the fingerprint of its invented host key; the project folders of PROJECT and
    OTHER hold their key tokens; the broker runs with the fake ssh and a fixed clock."""
    conf = tmp_path / "conf" / "keys.conf"
    conf.parent.mkdir()
    conf.write_text("region = eu-de\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    key = host_key()
    winlab.add(MACHINE, PORT, winbroker.fingerprint(key), True, today="2026-11-05")
    paths = config.Paths(shared=tmp_path / "shared", vault=tmp_path / "vault", projects_root=tmp_path / "projects")
    tokens = {}
    for code in (PROJECT, OTHER):
        folder = paths.projects_root / code
        (folder / ".git").mkdir(parents=True)
        (folder / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n" % (code, code), encoding="utf-8")
        tokens[code] = projects.write_key_token(folder)
    keyfile = tmp_path / "id_ed25519"
    keyfile.write_bytes(secrets.token_bytes(64))
    keyfile.chmod(0o600)
    fake = FakeSsh(key)
    clock = [T0]
    lines: list[str] = []
    broker = winbroker.Broker(tmp_path / "unused.sock", keyfile, registry=conf.parent / winlab.FILE_NAME,
                              paths_fn=lambda: paths, runner=fake, clock=lambda: clock[0], journal=lines.append)
    fake.broker, fake.clock, fake.tokens, fake.paths, fake.keyfile, fake.lines = (broker, clock, tokens, paths,
                                                                                  keyfile, lines)
    fake.registry = conf.parent / winlab.FILE_NAME
    yield fake
    if broker.known_dir is not None:
        shutil.rmtree(broker.known_dir, ignore_errors=True)


def call(lab, op="run", project=PROJECT, machine=MACHINE, token=None, payload=None, **fields):
    req = dict(fields, op=op, project=project, machine=machine,
               token=lab.tokens.get(project) if token is None else token)
    if op == "run":
        req.setdefault("text", "Get-VM")
    return lab.broker.handle(req, 1000, payload)


def refused(answer) -> str:
    assert answer[0]["ok"] is False and answer[0]["kind"] == "refused", answer
    assert "\n" not in answer[0]["error"]
    return answer[0]["error"]


def open_window(lab, minutes=60, project=PROJECT, machine=MACHINE):
    winlab.open_(machine, minutes, project, now=lab.clock[0])


# --------------------------------------------------------------------------- the refusals, one line each


def test_wl_broker_refuses_an_unregistered_machine(lab):
    open_window(lab)
    assert "%s is not a registered lab machine" % OTHER_MACHINE in refused(call(lab, machine=OTHER_MACHINE))
    assert lab.calls == []


def test_wl_broker_refuses_without_a_window(lab):
    assert "%s has no open window" % MACHINE in refused(call(lab))
    assert "has no open window" in refused(call(lab, "put_chunk", path="C:\\lab\\a.txt", offset=0, length=1))
    assert lab.calls == []


def test_wl_broker_refuses_an_expired_window(lab):
    open_window(lab, minutes=30)
    lab.clock[0] = T0 + datetime.timedelta(minutes=29)
    assert call(lab)[0]["ok"] is True
    lab.calls.clear()
    lab.clock[0] = T0 + datetime.timedelta(minutes=30)
    assert "the window of %s for %s ended at 2026-11-05 10:30 UTC" % (MACHINE, PROJECT) in refused(call(lab))
    assert lab.calls == []


def test_wl_broker_refuses_a_window_of_another_project(lab):
    open_window(lab, project=OTHER)
    line = refused(call(lab))
    assert "open for another project, not for %s" % PROJECT in line and OTHER not in line
    assert lab.calls == []


def test_wl_broker_refuses_a_host_key_that_does_not_match_the_pin(lab):
    open_window(lab)
    lab.scan_key = host_key()            # the port answers with another machine's key
    line = refused(call(lab))
    assert line == winbroker.MISMATCH % MACHINE
    assert [c[0] for c in lab.calls] == ["ssh-keyscan"] and lab.sent == []


def test_wl_broker_refuses_when_ssh_meets_another_host_key_than_the_pinned_one(lab):
    """The scan matched, then the connection that would carry the command met another key: ssh's strict check
    refuses it, the broker answers the same line and scans again next time."""
    open_window(lab)
    lab.ssh_error = b"Host key verification failed.\r\n"
    assert refused(call(lab)) == winbroker.MISMATCH % MACHINE
    assert lab.sent == [] and MACHINE not in lab.broker.pinned
    lab.ssh_error = None
    assert call(lab)[0]["ok"] is True
    assert [c[0] for c in lab.calls] == ["ssh-keyscan", "ssh", "ssh-keyscan", "ssh"]


def test_wl_broker_refuses_a_call_without_the_key_token_of_its_project(lab):
    open_window(lab)
    assert refused(call(lab, token="")) == winbroker.UNBOUND_LINE
    # a session of OTHER that names PROJECT: its own token is not the one of PROJECT
    assert refused(call(lab, token=lab.tokens[OTHER])) == winbroker.FOREIGN_LINE % PROJECT
    assert lab.calls == []


# --------------------------------------------------------------------------- what passes and how


def test_wl_broker_runs_a_command_over_ssh_with_the_pinned_key_only_and_no_key_comes_back(lab, tmp_path):
    winlab.remove(MACHINE)
    winlab.add(MACHINE, PORT, winbroker.fingerprint(lab.scan_key), True, today="2026-11-05", account="labops")
    open_window(lab)
    lab.reply = (0, "lab-vm running\r\n".encode("utf-8"), b"")
    answer, data = call(lab, text="Get-VM | Format-Table Name, State", timeout=60)
    assert answer == {"ok": True, "exit": 0, "stdout": "lab-vm running\r\n", "stderr": ""} and data == b""
    assert lab.sent == ["Get-VM | Format-Table Name, State"]
    argv = lab.ssh_calls()[0]
    for option in ("StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null", "BatchMode=yes",
                   "IdentitiesOnly=yes", "HostKeyAlias=awb-winlab-%s" % MACHINE, "HostKeyAlgorithms=ssh-ed25519"):
        assert option in argv
    assert argv[argv.index("-p") + 1] == str(PORT) and argv[argv.index("-i") + 1] == str(lab.keyfile)
    assert "labops@127.0.0.1" in argv and argv[-1].startswith("powershell.exe ")
    assert lab.known == ["awb-winlab-%s ssh-ed25519 %s\n" % (MACHINE, lab.scan_key)]
    # the key never travels back: neither its bytes nor its path
    raw = lab.keyfile.read_bytes()
    text = json.dumps(answer) + "\n".join(lab.lines)
    for value in (str(lab.keyfile), base64.b64encode(raw).decode(), raw.hex()):
        assert value not in text
    assert lab.lines[-1].endswith("uid=1000 op=run machine=%s project=%s ok" % (MACHINE, PROJECT))
    # a second call keeps the pin: no new scan
    call(lab)
    assert [c[0] for c in lab.calls] == ["ssh-keyscan", "ssh", "ssh"]


def test_wl_broker_puts_gets_and_stats_a_file_in_chunks(lab):
    open_window(lab)
    path = "C:\\lab\\a.txt.awb-part"
    first = call(lab, "put_chunk", path=path, offset=0, length=5, payload=lambda: b"hello")
    assert first[0] == {"ok": True, "size": 5}
    wrong = call(lab, "put_chunk", path=path, offset=2, length=1, payload=lambda: b"x")
    assert wrong[0]["kind"] == "offset" and wrong[0]["size"] == 5
    assert call(lab, "get_chunk", path=path, offset=1, size=3) == ({"ok": True, "length": 3}, b"ell")
    st = call(lab, "stat", path=path, digest=True)[0]["stat"]
    assert st["size"] == 5 and st["sha256"].lower() == hashlib.sha256(b"hello").hexdigest()
    assert call(lab, "stat", path="C:\\lab\\none.txt")[0]["stat"] is None
    assert "path on the machine" in refused(call(lab, "stat", path="C:\\lab\\a.txt:stream"))


def test_wl_broker_answers_offline_when_the_tunnel_is_down(lab):
    open_window(lab)
    lab.ssh_error = b"ssh: connect to host 127.0.0.1 port 2201: Connection refused\r\n"
    answer = call(lab)[0]
    assert answer["kind"] == "offline" and "does not answer on its port %d" % PORT in answer["error"]


def test_wl_broker_will_not_start_with_a_key_others_may_read(lab):
    lab.keyfile.chmod(0o640)
    with pytest.raises(winlab.WinlabError, match="no usable key"):
        lab.broker.start()


# --------------------------------------------------------------------------- the session over the socket


@pytest.fixture
def served(lab, monkeypatch):
    """The broker on a real Unix socket (a short path) and a session of PROJECT on a sealed host: the work user
    reads no registry of its own."""
    folder = Path(tempfile.mkdtemp(prefix="awb-wlb-"))
    sock = folder / "broker.sock"
    lab.broker.socket_path = sock
    lab.broker.start()
    monkeypatch.setenv(winlabsession.BROKER_SOCKET_ENV, str(sock))
    monkeypatch.setenv("AWB_KEYS_CONF", str(lab.paths.shared / "work" / "keys.conf"))
    yield lab
    lab.broker.stop()
    shutil.rmtree(folder, ignore_errors=True)


def test_wl_broker_socket_serves_the_session_transport(served):
    lab = served
    assert stat.S_IMODE(os.stat(lab.broker.socket_path).st_mode) == 0o660
    winlab.open_(MACHINE, 60, PROJECT, path=lab.registry, now=lab.clock[0])
    root = lab.paths.projects_root / PROJECT
    assert not winlabsession._local_registry()
    _root, code, machine = winlabsession._session(MACHINE, root)
    assert code == PROJECT and machine["name"] == MACHINE and "fingerprint" not in machine
    assert winlabsession._pick(root, None) == MACHINE
    transport = winlabsession.connect(machine)
    lab.reply = (3, b"out\n", b"err\n")
    assert transport.run("Get-VM", timeout=30) == (3, "out\n", "err\n")
    assert transport.put_chunk("C:\\lab\\b.bin", 0, b"\x00\x01" * 1000) == 2000
    with pytest.raises(ValueError):
        transport.put_chunk("C:\\lab\\b.bin", 10, b"x")
    assert transport.get_chunk("C:\\lab\\b.bin", 1998, 100) == b"\x00\x01"
    assert transport.stat("C:\\lab\\b.bin")["size"] == 2000
    # the session of OTHER gets the broker's refusal, nothing goes out
    sent = len(lab.sent)
    other = lab.paths.projects_root / OTHER
    with pytest.raises(winlab.WinlabError, match="open for another project, not for %s" % OTHER):
        winlabsession._session(MACHINE, other)
    with pytest.raises(winlab.WinlabError, match="no lab machine has an open window for %s" % OTHER):
        winlabsession._pick(other, None)
    # a refused put of a whole chunk still reads as the refusal, not as a dropped connection
    foreign = winlabsession.BrokerTransport(MACHINE, OTHER, projects.read_key_token(other))
    with pytest.raises(winlab.WinlabError, match="open for another project"):
        foreign.put_chunk("C:\\lab\\c.bin", 0, b"x" * winlabsession.CHUNK)
    assert len(lab.sent) == sent


def test_wl_broker_missing_socket_means_no_transport(lab, monkeypatch, tmp_path):
    monkeypatch.setenv(winlabsession.BROKER_SOCKET_ENV, str(tmp_path / "none.sock"))
    with pytest.raises(winlab.WinlabError, match="no transport to a lab machine yet"):
        winlabsession.connect({"name": MACHINE, "project": PROJECT, "root": str(tmp_path)})
    assert lab.calls == []


# --------------------------------------------------------------------------- awb owner status


def test_wl_owner_status_lists_every_registered_machine_that_is_online(lab):
    winlab.add(OTHER_MACHINE, 2202, winbroker.fingerprint(host_key()), True, today="2026-11-05")
    probed = []

    def probe(port):
        probed.append(port)
        return port == 2202

    items = ownerwait.winlab_online_items(probe=probe)
    assert sorted(probed) == [PORT, 2202]
    assert [(i.label, i.text) for i in items] == [(OTHER_MACHINE, "lab machine online, its tunnel answers on port "
                                                                  "2202")]
    assert ownerwait.winlab_online_items(probe=lambda port: False) == []
