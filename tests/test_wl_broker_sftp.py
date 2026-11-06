"""B-039: put_chunk of the lab broker over sftp (awb/tcp/winbroker.py). Windows sshd stops passing standard input to
powershell.exe after about 2 KB (live check of 2026-11-06), so the bytes of a chunk go with sftp to PART.awb-chunk
next to the part and one ssh run appends that file to the part.

A fake ssh and a fake sftp only (FakeLab below), never a real connection. The fake sftp reads its batch from
standard input as `sftp -b -` does, takes `put` as sftp does (the remote file is created or overwritten) and `put -a`
or `reput` as an append; the fake ssh runs the steps the broker's script names against the files in memory. Every
machine name, code, key and fingerprint is invented; the keys are made at run time.
"""
from __future__ import annotations

import base64
import datetime
import os
import re
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from awb import config, projects, winlab
from awb.tcp import winbroker

MACHINE = "lab-1"
PORT = 2201
PROJECT = "tcp-ab2c"
T0 = datetime.datetime(2026, 11, 6, 10, 0, tzinfo=datetime.timezone.utc)
PART = "C:\\lab\\big.bin.awb-part"
CHUNK = PART.lower() + ".awb-chunk"


def host_key() -> str:
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + secrets.token_bytes(32)
    return base64.b64encode(blob).decode("ascii")


class FakeLab:
    """ssh-keyscan answers `scan_key`; sftp takes its batch and keeps what it puts in `files` (or answers
    `sftp_reply`, or times out with `sftp_timeout`); ssh runs the broker's append script against `files`."""

    def __init__(self, key: str):
        self.scan_key = key
        self.calls: list[tuple[list[str], bytes]] = []
        self.files: dict[str, bytearray] = {}
        self.locals: list[dict] = []
        self.sftp_reply: tuple[int, bytes, bytes] | None = None
        self.sftp_timeout = False
        self.ssh_timeout = False

    def of(self, program: str) -> list[tuple[list[str], bytes]]:
        return [c for c in self.calls if c[0][0] == program]

    def __call__(self, argv, data, timeout):
        self.calls.append((list(argv), data))
        if argv[0] == "ssh-keyscan":
            port = argv[argv.index("-p") + 1]
            return 0, ("[127.0.0.1]:%s ssh-ed25519 %s\n" % (port, self.scan_key)).encode("ascii"), b""
        known = next(a.split("=", 1)[1] for a in argv if a.startswith("UserKnownHostsFile="))
        if argv[0] == "sftp":
            return self.sftp(argv, data, known)
        assert argv[0] == "ssh"
        if self.ssh_timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        cut = data.index(b"\n")
        script = data[cut + 1:cut + 1 + int(data[:cut])].decode("utf-8")
        assert len(data) == cut + 1 + int(data[:cut]), "bytes after the script on the standard input of ssh"
        return self.append(script)

    def sftp(self, argv, data, known):
        batch = data.decode("utf-8")
        local = None
        for line in batch.splitlines():
            m = re.match(r'^(put -a|reput|put) "([^"]+)" "([^"]+)"$', line)
            if not m:
                assert line.startswith('-mkdir "/'), line
                continue
            local = Path(m.group(2))
            st = os.lstat(local)
            self.locals.append({"path": local, "mode": stat.S_IMODE(st.st_mode),
                                "folder": stat.S_IMODE(os.lstat(local.parent).st_mode),
                                "known": Path(known).read_text(encoding="ascii")})
            if self.sftp_timeout:
                raise subprocess.TimeoutExpired(argv, 1)
            if self.sftp_reply is not None:
                return self.sftp_reply
            remote = m.group(3)
            assert remote.startswith("/") and "\\" not in remote
            name = remote[1:].replace("/", "\\").lower()
            if m.group(1) == "put":
                self.files[name] = bytearray(local.read_bytes())
            else:
                self.files.setdefault(name, bytearray()).extend(local.read_bytes())
        assert local is not None
        return 0, b"sftp> put\n", b""

    def append(self, script):
        """The steps of the broker's put script, as far as the script names them."""
        def value(name):
            return re.search(r"^\$%s = (.*)$" % name, script, re.M).group(1)

        p = value("p")[1:-1].replace("''", "'").lower()
        c = value("c")[1:-1].replace("''", "'").lower()
        o = int(re.search(r"\d+", value("o")).group(0))
        want = int(re.search(r"\d+", value("want")).group(0))
        deletes = "[IO.File]::Delete($c)" in script
        code, size = 0, 0
        if o != 0 and p not in self.files:
            code = 5
        elif len(self.files.get(c, b"")) != want and "-ne $want" in script:
            code = 6
        else:
            part = self.files.setdefault(p, bytearray())
            if o == 0 and "[IO.FileMode]::Create" in script:
                part.clear()
            if len(part) != o and "if ($n -ne $o) { $code = 5 }" in script:
                code, size = 5, len(part)
            else:
                part += self.files.get(c, b"")
                size = len(part)
        if deletes:
            self.files.pop(c, None)
        return code, (b"size=%d" % size if code in (0, 5) else b""), b""


@pytest.fixture
def lab(tmp_path, monkeypatch):
    conf = tmp_path / "conf" / "keys.conf"
    conf.parent.mkdir()
    conf.write_text("region = eu-de\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    key = host_key()
    winlab.add(MACHINE, PORT, winbroker.fingerprint(key), True, today="2026-11-06")
    paths = config.Paths(shared=tmp_path / "shared", vault=tmp_path / "vault", projects_root=tmp_path / "projects")
    folder = paths.projects_root / PROJECT
    (folder / ".git").mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n" % (PROJECT, PROJECT), encoding="utf-8")
    token = projects.write_key_token(folder)
    keyfile = tmp_path / "id_ed25519"
    keyfile.write_bytes(secrets.token_bytes(64))
    keyfile.chmod(0o600)
    fake = FakeLab(key)
    broker = winbroker.Broker(tmp_path / "unused.sock", keyfile, registry=conf.parent / winlab.FILE_NAME,
                              paths_fn=lambda: paths, runner=fake, clock=lambda: T0, journal=lambda line: None)
    winlab.open_(MACHINE, 60, PROJECT, now=T0)
    fake.broker, fake.token, fake.keyfile = broker, token, keyfile
    yield fake
    if broker.known_dir is not None:
        shutil.rmtree(broker.known_dir, ignore_errors=True)


def put(lab, offset, data, path=PART):
    req = {"op": "put_chunk", "project": PROJECT, "machine": MACHINE, "token": lab.token, "path": path,
           "offset": offset, "length": len(data)}
    return lab.broker.handle(req, 1000, lambda: data)[0]


def test_wl_put_chunk_goes_over_sftp_with_the_pinned_options_and_no_data_on_the_standard_input_of_ssh(lab):
    data = secrets.token_bytes(64 * 1024)
    assert put(lab, 0, data) == {"ok": True, "size": len(data)}
    assert bytes(lab.files[PART.lower()]) == data and CHUNK not in lab.files
    [(argv, batch)] = lab.of("sftp")
    assert argv[argv.index("-P") + 1] == str(PORT) and argv[argv.index("-i") + 1] == str(lab.keyfile)
    assert argv[argv.index("-b") + 1] == "-" and argv[-1] == "%s@127.0.0.1" % winlab.ACCOUNT
    for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null",
                   "IdentitiesOnly=yes", "HostKeyAlias=awb-winlab-%s" % MACHINE, "HostKeyAlgorithms=ssh-ed25519"):
        assert option in argv
    assert lab.locals[0]["known"] == "awb-winlab-%s ssh-ed25519 %s\n" % (MACHINE, lab.scan_key)
    assert '"/C:/lab/big.bin.awb-part.awb-chunk"' in batch.decode("utf-8") and data[:32] not in batch
    # ssh carried the script only: small, and none of the bytes of the chunk
    [(ssh_argv, stdin)] = lab.of("ssh")
    assert len(stdin) < 4096 and data[:32] not in stdin and data[-32:] not in stdin
    assert ssh_argv[ssh_argv.index("-p") + 1] == str(PORT)
    # the next chunk appends
    more = secrets.token_bytes(1000)
    assert put(lab, len(data), more) == {"ok": True, "size": len(data) + 1000}
    assert bytes(lab.files[PART.lower()]) == data + more


def test_wl_put_chunk_temporary_file_is_mode_600_and_gone_after_success_failure_and_timeout(lab):
    assert put(lab, 0, b"hello")["ok"] is True
    lab.sftp_reply = (1, b"", b'remote open("/C:/lab/big.bin.awb-part.awb-chunk"): Failure\r\n')
    assert put(lab, 5, b" world")["kind"] == "failed"
    lab.sftp_reply = None
    lab.sftp_timeout = True
    assert put(lab, 5, b" world")["kind"] == "offline"
    assert len(lab.locals) == 3
    for seen in lab.locals:
        assert seen["mode"] == 0o600 and seen["folder"] == 0o700
        assert not seen["path"].exists()
    assert list(lab.locals[0]["path"].parent.iterdir()) == []
    assert bytes(lab.files[PART.lower()]) == b"hello"


def test_wl_put_chunk_at_another_offset_than_the_size_of_the_part_is_refused_and_leaves_it(lab):
    assert put(lab, 0, b"hello") == {"ok": True, "size": 5}
    answer = put(lab, 2, b"xyz")
    assert answer["ok"] is False and answer["kind"] == "offset" and answer["size"] == 5
    assert bytes(lab.files[PART.lower()]) == b"hello" and CHUNK not in lab.files
    missing = put(lab, 7, b"x", path="C:\\lab\\none.bin.awb-part")
    assert missing["kind"] == "offset" and missing["size"] == 0


def test_wl_put_chunk_sftp_failure_is_offline_when_ssh_cannot_reach_the_machine_and_failed_otherwise(lab):
    lab.sftp_reply = (255, b"", b"ssh: connect to host 127.0.0.1 port 2201: Connection refused\r\n"
                                b"Connection closed\r\n")
    answer = put(lab, 0, b"hello")
    assert answer["kind"] == "offline" and "does not answer on its port %d" % PORT in answer["error"]
    lab.sftp_reply = (1, b"", b'dest open "/C:/lab/big.bin.awb-part.awb-chunk": Permission denied\r\n')
    answer = put(lab, 0, b"hello")
    assert answer["kind"] == "failed" and "\n" not in answer["error"]
    assert lab.of("ssh") == [] and PART.lower() not in lab.files
    lab.sftp_reply = (255, b"", b"Host key verification failed.\r\nConnection closed\r\n")
    assert put(lab, 0, b"hello")["error"] == winbroker.MISMATCH % MACHINE
    assert MACHINE not in lab.broker.pinned


def test_wl_put_chunk_stale_chunk_file_does_not_end_up_in_the_part_twice(lab):
    assert put(lab, 0, b"hello") == {"ok": True, "size": 5}
    # a broken call: sftp put the chunk, then the ssh run that appends it was cut
    lab.ssh_timeout = True
    assert put(lab, 5, b" world")["kind"] == "offline"
    assert bytes(lab.files[CHUNK]) == b" world" and bytes(lab.files[PART.lower()]) == b"hello"
    lab.ssh_timeout = False
    assert put(lab, 5, b" world") == {"ok": True, "size": 11}
    assert bytes(lab.files[PART.lower()]) == b"hello world" and CHUNK not in lab.files
    # a stale chunk of another call is overwritten, not appended to
    lab.files[CHUNK] = bytearray(b"STALE")
    assert put(lab, 11, b"!") == {"ok": True, "size": 12}
    assert bytes(lab.files[PART.lower()]) == b"hello world!"


def test_wl_put_chunk_path_that_sftp_cannot_take_is_refused_before_anything_is_sent(lab):
    assert winbroker.sftp_path("C:\\lab\\a b\\c.txt") == "/C:/lab/a b/c.txt"
    for path in ("C:\\lab\\\\a.bin", "C:\\lab\\a.bin\x7f", "C:\\lab\\a/\\b.bin"):
        answer = put(lab, 0, b"x", path=path)
        assert answer["kind"] == "refused" and answer["error"] == winbroker.NO_SFTP_PATH, path
    assert lab.calls == []
