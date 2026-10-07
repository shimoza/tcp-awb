"""vault: gpg encryption with a private homedir, the daemon on two unix sockets, the client.

Sockets live under a short folder from tempfile.mkdtemp(prefix="awb", dir="/tmp") and are removed afterwards.
Every daemon started here is stopped at the end of its test. No gpg agent is left behind. Names come from
tests/fixtures.py only.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awb import check, config, normalize, register, vault
from tests import fixtures

ROOT = Path(__file__).resolve().parent.parent
PASS = "fixture passphrase of the tests 7"
SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
"""The shape of the lock time, written here again on purpose: a daemon that drifts from it fails here."""
WRONG = "not the passphrase of the tests"
TEXT = "offer to %s, attention %s, host %s\n" % (
    fixtures.CUSTOMER_FORMS[0], fixtures.PERSON_FORMS[0], "198.51.100.20")
SECRETS = fixtures.ALL_REGISTERED + [fixtures.CUSTOMER_CODE, fixtures.PERSON_CODE, fixtures.ORG_CODE, PASS, WRONG]


# --------------------------------------------------------------------------- helpers (also used by other test files)


@contextlib.contextmanager
def short_dir():
    """A short private folder for sockets and gpg homedirs, removed afterwards."""
    d = tempfile.mkdtemp(prefix="awb", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def vault_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("awb-vault")]


@contextlib.contextmanager
def serving(p: config.Paths, **kwargs):
    """A vault daemon in threads of this process, stopped at the end."""
    d = vault.Daemon(p, **kwargs)
    d.start()
    try:
        yield d
    finally:
        d.stop()
        assert not vault_threads(), "a thread of the vault daemon is still running"
        assert not d.admin_path.exists() and not d.check_path.exists()


@pytest.fixture
def sockets(monkeypatch):
    """Short socket paths in the environment: the check socket in a folder that does not exist yet."""
    with short_dir() as d:
        monkeypatch.setenv("AWB_CHECK_SOCKET", str(d / "run" / "check.sock"))
        monkeypatch.setenv("AWB_ADMIN_SOCKET", str(d / "admin.sock"))
        yield d


@pytest.fixture
def fast_gpg(monkeypatch):
    """The lowest count gpg takes without an agent, so that the tests stay fast."""
    monkeypatch.setattr(vault, "S2K_COUNT", 65536)


@pytest.fixture
def vp(home, sockets, fast_gpg) -> config.Paths:
    return config.paths()


@pytest.fixture
def daemon(vp):
    with serving(vp) as d:
        yield d


def encrypt_vault(monkeypatch, passphrase: str = PASS, again: str | None = None) -> int:
    """Run `awb vault encrypt --stdin` with the passphrase typed twice."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("%s\n%s\n" % (passphrase, passphrase if again is None else again)))
    return vault.main(["encrypt", "--stdin"])


def unlock(monkeypatch, passphrase: str = PASS) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(passphrase + "\n"))
    return vault.main(["unlock", "--stdin"])


def raw(sock: Path, payload) -> tuple[dict, bytes]:
    """One request as the bytes on the wire; the parsed answer and its raw bytes."""
    line = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8") + b"\n"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(sock))
    buf = b""
    try:
        s.sendall(line)
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf), buf


def assert_clean(text: str, where: str) -> None:
    fixtures.assert_no_fixture_name(text, where)
    for s in SECRETS:
        assert s not in text, "a value in %s" % where


def gpg_agents(homedir: Path) -> list[str]:
    if shutil.which("pgrep") is None:
        return []
    out = subprocess.run(["pgrep", "-f", "--", str(homedir)], capture_output=True, text=True, check=False)
    return out.stdout.split()


# --------------------------------------------------------------------------- encryption


def test_encrypt_and_decrypt_round_trip():
    data = ("\n".join(fixtures.register_lines()) + "\n").encode("utf-8")
    with short_dir() as d:
        home = d / "gnupg"
        enc = vault.encrypt_bytes(data, PASS, home)
        assert enc and enc != data
        assert_clean(enc.decode("latin-1"), "the encrypted bytes")
        assert vault.decrypt_bytes(enc, PASS, home) == data
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        # no plaintext temporary file next to the homedir and no agent was started
        assert [p.name for p in d.iterdir()] == ["gnupg"]
        assert not gpg_agents(home)


def test_wrong_passphrase_or_damaged_data_raises_without_a_value(fast_gpg):
    data = TEXT.encode("utf-8")
    with short_dir() as d:
        home = d / "gnupg"
        enc = vault.encrypt_bytes(data, PASS, home)
        damaged = bytearray(enc)
        damaged[len(damaged) // 2] ^= 0xFF
        literal = subprocess.run(["gpg", "--batch", "--quiet", "--no-autostart", "--homedir", str(home), "--store",
                                  "--output", "-"], input=data, capture_output=True, check=True).stdout
        for bad, pw in ((enc, WRONG), (bytes(damaged), PASS), (data, PASS), (literal, PASS)):
            with pytest.raises(vault.VaultError) as err:
                vault.decrypt_bytes(bad, pw, home)
            assert str(err.value) == "wrong passphrase or damaged file"
        for pw in ("", "two\nlines"):
            with pytest.raises(vault.VaultError) as err:
                vault.encrypt_bytes(data, pw, home)
            assert_clean(str(err.value), "the passphrase error")


def test_the_passphrase_goes_through_its_own_pipe(fast_gpg, monkeypatch):
    calls = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(vault.subprocess, "run", spy)
    with short_dir() as d:
        enc = vault.encrypt_bytes(TEXT.encode(), PASS, d / "g")
        vault.decrypt_bytes(enc, PASS, d / "g")
    assert len(calls) == 2
    for cmd, kwargs in calls:
        for flag in ("--batch", "--no-symkey-cache", "--homedir"):
            assert flag in cmd
        assert cmd[cmd.index("--pinentry-mode") + 1] == "loopback"
        fd = int(cmd[cmd.index("--passphrase-fd") + 1])
        assert kwargs["pass_fds"] == (fd,)
        assert all(PASS not in a for a in cmd)
        assert all(PASS not in v for v in kwargs["env"].values())
    assert calls[0][1]["input"] == TEXT.encode()
    assert "AES256" in calls[0][0]


# --------------------------------------------------------------------------- the daemon, plain state


def test_plain_state_checks_without_unlock(vp, daemon):
    assert vault.ping(vp.check_socket) == "plain"
    hits = vault.check_remote(TEXT, vp.check_socket)
    assert hits == check.check_text(TEXT, vp.register)
    assert [h["cls"] for h in hits] == ["name", "name", "ip"]
    assert all(set(h) == {"start", "length", "cls"} for h in hits)


def test_socket_modes_and_the_check_folder(vp, daemon):
    assert stat.S_IMODE(vp.admin_sock.stat().st_mode) == 0o600
    assert stat.S_IMODE(vp.check_socket.stat().st_mode) == 0o660
    assert stat.S_ISSOCK(vp.check_socket.stat().st_mode)
    assert stat.S_IMODE(vp.check_socket.parent.stat().st_mode) == 0o750
    assert vp.check_socket.stat().st_gid == os.getegid()
    with pytest.raises(vault.VaultError) as err:
        vault.Daemon(vp).start()
    assert "another vault daemon" in str(err.value)
    assert vault.ping(vp.check_socket) == "plain"


def test_the_check_socket_takes_check_and_ping_only(vp, daemon):
    for op in ("status", "unlock", "register_load", "lock"):
        assert raw(vp.check_socket, {"op": op})[0] == {"ok": False, "error": "unknown op"}
    assert raw(vp.check_socket, b"not json\n")[0] == {"ok": False, "error": "bad request"}
    assert raw(vp.check_socket, {"op": "check", "text": 7})[0] == {"ok": False, "error": "bad request"}
    answer, wire = raw(vp.check_socket, {"op": "ping"})
    assert answer == {"ok": True, "state": "plain"}


def test_status_prints_state_and_counts_never_a_form(vp, daemon, capsys):
    assert vault.main(["status"]) == 0
    out, err = capsys.readouterr()
    entries = register.load(vp.register)
    assert out.splitlines() == ["state: plain", "codes: %d" % len(register.codes(entries)),
                                "forms: %d" % len(entries)]
    assert_clean(out + err, "status output")


# --------------------------------------------------------------------------- encrypt, lock, unlock


def _plain_extras(vp: config.Paths) -> dict[Path, bytes]:
    files = {
        vp.keep_list: ("%s\n" % fixtures.CONTROL_UNREGISTERED).encode(),
        vp.originals / fixtures.CUSTOMER_CODE / "F-AB2C.txt": TEXT.encode(),
        vp.private_reports / fixtures.CUSTOMER_CODE / "2026-09-22-1010.md": ("# report\n" + TEXT).encode(),
    }
    for f, data in files.items():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    files[vp.register] = vp.register.read_bytes()
    return files


def test_encrypt_removes_the_plaintext_and_unlocks_the_daemon(vp, daemon, monkeypatch, capsys):
    files = _plain_extras(vp)
    expected = check.check_text(TEXT, vp.register)
    assert encrypt_vault(monkeypatch) == 0
    out, err = capsys.readouterr()
    assert "encrypted 4 files" in out and "vault unlocked" in out
    assert_clean(out + err, "encrypt output")
    for f, data in files.items():
        enc = f.with_name(f.name + ".gpg")
        assert not f.exists(), "a plaintext file is left"
        assert stat.S_IMODE(enc.stat().st_mode) == 0o600
        assert vault.decrypt_bytes(enc.read_bytes(), PASS, vp.vault / ".gnupg") == data
    assert stat.S_IMODE((vp.vault / ".gnupg").stat().st_mode) == 0o700
    assert daemon.state() == "unlocked"
    assert vault.check_remote(TEXT, vp.check_socket) == expected
    assert check.check_text(TEXT, None) == expected
    # once is enough
    assert encrypt_vault(monkeypatch) == 2
    assert "already encrypted" in capsys.readouterr().err


def test_encrypt_refuses_differing_or_short_passphrases(vp, monkeypatch, capsys):
    before = vp.register.read_bytes()
    assert encrypt_vault(monkeypatch, PASS, WRONG) == 2
    assert encrypt_vault(monkeypatch, "short", "short") == 2
    err = capsys.readouterr().err
    assert "differ" in err and "at least" in err
    assert_clean(err, "encrypt errors")
    assert vp.register.read_bytes() == before and not vp.register_encrypted.exists()


def test_without_a_daemon_encrypt_still_seals_and_says_so(vp, monkeypatch, capsys):
    assert encrypt_vault(monkeypatch) == 0
    assert "no vault daemon answers" in capsys.readouterr().out
    assert vp.register_encrypted.exists() and not vp.register.exists()


def test_locked_answers_locked_with_its_time(vp, daemon, monkeypatch, capsys):
    assert encrypt_vault(monkeypatch) == 0
    assert vault.main(["lock"]) == 0
    assert daemon.state() == "locked"
    assert vault.ping(vp.check_socket) == "locked"
    answer = raw(vp.check_socket, {"op": "check", "text": TEXT})[0]
    # T3 (build/DECISIONS.md, 2026-10-07, D-T3b): the locked answer carries the time of the lock
    assert set(answer) == {"ok", "error", "since"} and answer["ok"] is False and answer["error"] == "locked"
    assert SINCE_RE.fullmatch(answer["since"])
    with pytest.raises(vault.VaultLocked):
        vault.check_remote(TEXT, vp.check_socket)
    with pytest.raises(check.CheckUnavailable) as err:
        check.check_text(TEXT, None)
    assert str(err.value) == "name check unavailable: vault locked"
    with pytest.raises(vault.VaultLocked):
        vault.admin_call("register_load")
    assert vault.main(["status"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "state: locked"


def test_the_daemon_answers_since_while_locked_only(vp, monkeypatch, capsys):
    """T3. Planted failures: a daemon that never sets the time, never clears it on unlock, answers it while unlocked
    or plain, or writes it in another shape (SINCE_RE is written in this file, not taken from vault.py)."""
    def answers(sock: Path) -> list[dict]:
        ping = raw(sock, {"op": "ping"})[0]
        status = vault.admin_call("status")
        check_answer = raw(sock, {"op": "check", "text": "a text"})[0]
        return [ping, status, check_answer]

    with serving(vp) as d:
        assert all("since" not in a for a in answers(vp.check_socket))              # plain
        assert encrypt_vault(monkeypatch) == 0                                      # encrypt unlocks
        assert all("since" not in a for a in answers(vp.check_socket))
        assert vault.ping_state(vp.check_socket) == {"state": "unlocked", "since": None}
        assert vault.main(["lock"]) == 0
        locked = answers(vp.check_socket)
        since = locked[0].get("since")
        assert isinstance(since, str) and SINCE_RE.fullmatch(since)
        assert [a.get("since") for a in locked] == [since] * 3
        assert locked[2]["error"] == "locked"
        assert vault.ping_state(vp.check_socket) == {"state": "locked", "since": since}
        assert abs(datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                   - time.time()) < 60
        assert vault.main(["lock"]) == 0                                            # a second lock keeps its time
        assert vault.ping_state(vp.check_socket)["since"] == since
        capsys.readouterr()
        assert vault.main(["status"]) == 0
        assert capsys.readouterr().out.splitlines()[0] == "locked since %s" % since
        assert unlock(monkeypatch) == 0
        assert all("since" not in a for a in answers(vp.check_socket))
        assert d.state() == "unlocked"
    # a start with an encrypted register is locked from the start, with its own time
    with serving(vp) as d:
        state = vault.ping_state(vp.check_socket)
        assert state["state"] == "locked" and SINCE_RE.fullmatch(state["since"])


def test_the_locked_check_answer_carries_since(vp, daemon, monkeypatch):
    """T3. Planted failures: _error("locked") without the key; ping_state without it; a client that keeps it from
    an answer that is not locked."""
    assert vault._error("locked", since="2026-10-07T06:02:11Z") == {"ok": False, "error": "locked",
                                                                    "since": "2026-10-07T06:02:11Z"}
    assert "since" not in vault._error("rate", since="2026-10-07T06:02:11Z")
    assert encrypt_vault(monkeypatch) == 0
    assert vault.main(["lock"]) == 0
    with pytest.raises(vault.VaultLocked) as err:
        vault.check_remote(TEXT, vp.check_socket)
    assert SINCE_RE.fullmatch(err.value.since)
    assert vault.ping_state(vp.check_socket) == {"state": "locked", "since": err.value.since}
    with pytest.raises(check.CheckUnavailable) as unavailable:
        check.check_text(TEXT, None)
    assert unavailable.value.since == err.value.since
    with short_dir() as d:
        sock = d / "c.sock"
        with fake_daemon(sock, b'{"ok":true,"state":"unlocked","since":"2026-10-07T06:02:11Z"}\n'):
            assert vault.ping_state(sock) == {"state": "unlocked", "since": None}
            assert vault.ping(sock) == "unlocked"          # an extra key never breaks the plain ping


def test_unlock_then_check_answers_start_length_and_class_only(vp, daemon, monkeypatch, capsys):
    assert encrypt_vault(monkeypatch) == 0
    assert vault.main(["lock"]) == 0
    assert unlock(monkeypatch) == 0
    assert daemon.state() == "unlocked"
    answer, wire = raw(vp.check_socket, {"op": "check", "text": TEXT})
    assert set(answer) == {"ok", "hits"} and answer["ok"] is True
    assert all(set(h) == {"start", "length", "cls"} for h in answer["hits"])
    n = normalize.normalize(TEXT).text
    names = [n[h["start"]:h["start"] + h["length"]] for h in answer["hits"] if h["cls"] == "name"]
    assert names == [fixtures.CUSTOMER_FORMS[0], fixtures.PERSON_FORMS[0]]
    assert_clean(wire.decode("utf-8"), "the check answer")
    assert_clean(capsys.readouterr().out, "unlock output")


def test_a_wrong_passphrase_keeps_the_vault_locked(vp, daemon, monkeypatch, capsys):
    assert encrypt_vault(monkeypatch) == 0
    assert vault.main(["lock"]) == 0
    capsys.readouterr()
    assert unlock(monkeypatch, WRONG) == 2
    out, err = capsys.readouterr()
    assert "wrong passphrase" in err
    assert_clean(out + err, "unlock error")
    assert daemon.state() == "locked"
    assert raw(vp.admin_sock, {"op": "unlock", "passphrase": ""})[0]["error"] == "bad request"
    assert daemon.state() == "locked"


# --------------------------------------------------------------------------- log and limits


def test_the_check_log_carries_no_text(vp, daemon):
    vault.check_remote(TEXT, vp.check_socket)
    vault.check_remote("nothing to see here", vp.check_socket)
    log = vp.check_log / ("checks-%s.tsv" % datetime.now(timezone.utc).strftime("%Y-%m"))
    content = log.read_text(encoding="ascii")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    lines = content.splitlines()
    assert lines[0] == "time\tuid\tlength\thits\tresult"
    rows = [line.split("\t") for line in lines[1:]]
    assert [(r[1], r[2], r[3], r[4]) for r in rows] == [
        (str(os.getuid()), str(len(TEXT)), "3", "ok"), (str(os.getuid()), "19", "0", "ok")]
    assert_clean(content, "the check log")
    assert "nothing" not in content


def test_rate_limit_per_peer_uid(vp):
    with serving(vp, rate_limit=3) as d:
        for _ in range(2):
            assert vault.ping(vp.check_socket) == "plain"
        assert vault.check_remote(TEXT, vp.check_socket)
        assert raw(vp.check_socket, {"op": "ping"})[0] == {"ok": False, "error": "rate"}
        with pytest.raises(vault.VaultRateLimited):
            vault.check_remote(TEXT, vp.check_socket)
        assert issubclass(vault.VaultRateLimited, vault.VaultUnavailable)
        # the admin socket has no rate limit
        assert vault.admin_call("status")["state"] == "plain"
        assert d.state() == "plain"
    log = next(vp.check_log.glob("checks-*.tsv")).read_text(encoding="ascii")
    assert log.splitlines()[-1].split("\t")[2:] == [str(len(TEXT)), "-", "rate"]


def test_request_size_and_read_timeout(vp, monkeypatch):
    monkeypatch.setattr(vault, "MAX_REQUEST", 1000)
    monkeypatch.setattr(vault, "READ_TIMEOUT", 0.3)
    with serving(vp) as d:
        assert raw(vp.check_socket, b"x" * 3000)[0] == {"ok": False, "error": "too large"}
        with pytest.raises(vault.VaultUnavailable):
            vault.check_remote("y" * 2000, vp.check_socket)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(vp.check_socket))
        started = time.monotonic()
        try:
            assert s.recv(10) == b""
        finally:
            s.close()
        assert time.monotonic() - started < 3
        assert d.state() == "plain"


def test_the_admin_socket_refuses_another_user(vp, daemon, monkeypatch):
    monkeypatch.setattr(vault, "_peer_uid", lambda conn: os.getuid() + 1)
    assert raw(vp.admin_sock, {"op": "status"})[0] == {"ok": False, "error": "refused"}
    with pytest.raises(vault.VaultUnavailable):
        vault.admin_call("status")
    # the check socket serves every uid that can reach it
    assert vault.ping(vp.check_socket) == "plain"


# --------------------------------------------------------------------------- admin: register, seal_file, open_file


def test_admin_register_load_and_save_round_trip(vp, daemon, monkeypatch):
    assert encrypt_vault(monkeypatch) == 0
    before = register.load(vp.register)
    assert before == register.parse("\n".join(fixtures.register_lines()) + "\n")
    text = vault.admin_call("register_load")["text"]
    assert register.parse(text) == before
    enc_before = vp.register_encrypted.read_bytes()
    added = register.add(vp.register, fixtures.PARTNER_CODE + "-PERS-1", "PERS", fixtures.PLANTED_PERSON,
                         fixtures.TODAY)
    assert not vp.register.exists(), "a plaintext register was written"
    assert vp.register_encrypted.read_bytes() != enc_before
    assert register.load(vp.register) == before + [added]
    plain = vault.decrypt_bytes(vp.register_encrypted.read_bytes(), PASS, vp.vault / ".gnupg").decode()
    assert register.parse(plain) == before + [added]
    # the matcher of the daemon was reloaded
    hits = vault.check_remote("met %s today" % fixtures.PLANTED_PERSON, vp.check_socket)
    assert [h["cls"] for h in hits] == ["name"]
    assert vault.admin_call("status")["forms"] == len(before) + 1


def test_register_save_validates_and_refuses_without_the_form(vp, daemon, monkeypatch):
    assert encrypt_vault(monkeypatch) == 0
    good = vault.admin_call("register_load")["text"]
    bad_form = fixtures.CUSTOMER_FORMS[1] + " (note)"
    bad = good + "\t".join((fixtures.CUSTOMER_CODE, "CUST", bad_form, fixtures.TODAY, "active")) + "\n"
    with pytest.raises(vault.VaultError) as err:
        vault.admin_call("register_save", text=bad)
    msg = str(err.value)
    assert "line %d" % (len(fixtures.register_lines()) + 1) in msg
    assert_clean(msg, "the refusal")
    answer, wire = raw(vp.admin_sock, {"op": "register_save", "text": bad})
    assert answer["ok"] is False and answer["error"] == "invalid"
    assert_clean(wire.decode("utf-8"), "the refusal on the wire")
    # the register only grows: dropping a form is refused
    shorter = "\n".join(good.splitlines()[:-1]) + "\n"
    with pytest.raises(vault.VaultError) as err:
        vault.admin_call("register_save", text=shorter)
    assert "drops 1 forms" in str(err.value)
    assert vault.admin_call("register_load")["text"] == good
    with pytest.raises(register.RegisterError) as err:
        register.save(vp.register, register.parse(shorter))
    assert_clean(str(err.value), "the register error")


def test_seal_file_and_open_file(vp, daemon, monkeypatch):
    with pytest.raises(vault.VaultError):
        vault.admin_call("seal_file", path=str(vp.register))   # plain vault: nothing to seal with
    assert encrypt_vault(monkeypatch) == 0
    f = vp.originals / fixtures.CUSTOMER_CODE / "F-Q2RS.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(TEXT, encoding="utf-8")
    assert vault.admin_call("seal_file", path=str(f)) == {"ok": True}
    enc = f.with_name(f.name + ".gpg")
    assert not f.exists() and enc.exists()
    data = vault.admin_call("open_file", path=str(enc))["data"]
    assert base64.b64decode(data).decode("utf-8") == TEXT
    with short_dir() as outside:
        stray = outside / (fixtures.CUSTOMER_FORMS[1] + ".txt")
        stray.write_text("x", encoding="utf-8")
        link = vp.originals / "link.txt"
        link.symlink_to(stray)
        for path in (stray, link, vp.register_encrypted, "relative.txt"):
            with pytest.raises(vault.VaultError) as err:
                vault.admin_call("seal_file", path=str(path))
            assert_clean(str(err.value), "the seal refusal")
        assert stray.exists()
    assert vault.main(["lock"]) == 0
    with pytest.raises(vault.VaultLocked):
        vault.admin_call("open_file", path=str(enc))


# --------------------------------------------------------------------------- the client


@contextlib.contextmanager
def fake_daemon(sock: Path, answer: bytes):
    """A socket that answers every request with the same bytes."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(4)
    srv.settimeout(0.1)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(5)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                conn.sendall(answer)

    t = threading.Thread(target=loop, name="fake-vault")
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(5)
        srv.close()
        os.unlink(sock)


@pytest.mark.parametrize("answer", [
    {"ok": True, "hits": [{"start": 0, "length": 5, "cls": "name", "form": fixtures.CUSTOMER_FORMS[1]}]},
    {"ok": True, "hits": [{"start": 0, "length": 5, "cls": "name", "code": fixtures.CUSTOMER_CODE}]},
    {"ok": True, "hits": [{"start": 0, "length": 5}]},
    {"ok": True, "hits": [], "codes": 3},
    {"ok": True, "hits": [{"start": "0", "length": 5, "cls": "name"}]},
    {"ok": True, "hits": [{"start": True, "length": 5, "cls": "name"}]},
    {"ok": True, "hits": [{"start": 0, "length": 5, "cls": fixtures.CUSTOMER_FORMS[1].lower()}]},
    {"ok": True, "hits": {"start": 0}},
    {"ok": False, "error": fixtures.CUSTOMER_FORMS[0]},
    {"ok": False, "error": fixtures.CUSTOMER_FORMS[1].lower()},
    {"ok": False, "error": "invalid", "detail": fixtures.CUSTOMER_FORMS[1]},
    {"ok": False, "error": [fixtures.CUSTOMER_FORMS[1]]},
    "a plain line",
], ids=["form", "code", "no-class", "extra-top-key", "string-start", "bool-start", "class-value", "not-a-list",
        "error-value", "error-word", "error-detail", "error-list", "not-an-object"])
def test_the_client_refuses_any_other_answer(answer):
    with short_dir() as d:
        sock = d / "c.sock"
        with fake_daemon(sock, json.dumps(answer).encode("utf-8") + b"\n"):
            with pytest.raises(vault.VaultUnavailable) as err:
                vault.check_remote(TEXT, sock)
    assert_clean(str(err.value), "the client error")


def test_the_client_takes_a_clean_answer():
    answer = {"ok": True, "hits": [{"start": 9, "length": 5, "cls": "name"}, {"start": 20, "length": 3, "cls": "ip"}]}
    with short_dir() as d:
        sock = d / "c.sock"
        with fake_daemon(sock, json.dumps(answer).encode() + b"\n"):
            assert vault.check_remote(TEXT, sock) == answer["hits"]


def test_the_client_without_a_daemon():
    with short_dir() as d:
        with pytest.raises(vault.VaultUnavailable) as err:
            vault.check_remote(TEXT, d / "none.sock")
        assert str(err.value) == "no vault daemon"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(d / "stale.sock"))
        stale.close()
        with pytest.raises(vault.VaultUnavailable):
            vault.check_remote(TEXT, d / "stale.sock")
        with pytest.raises(vault.VaultUnavailable):
            vault.admin_call("status", d / "none.sock")


# --------------------------------------------------------------------------- awb vault serve


def test_serve_runs_until_sigterm_and_removes_its_sockets(vp):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([sys.executable, "-m", "awb", "vault", "serve"], env=env, cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while not (vp.check_socket.exists() and vp.admin_sock.exists()):
            assert proc.poll() is None, "the daemon ended early"
            assert time.monotonic() < deadline, "the daemon did not start"
            time.sleep(0.05)
        assert vault.ping(vp.check_socket) == "plain"
        assert vault.check_remote(TEXT, vp.check_socket) == check.check_text(TEXT, vp.register)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0
    assert not vp.check_socket.exists() and not vp.admin_sock.exists()
    assert_clean((out + err).decode("utf-8"), "serve output")


def test_usage_errors_exit_2(capsys):
    assert vault.main([]) == 2
    assert vault.main(["--" + fixtures.CUSTOMER_FORMS[1]]) == 2
    assert_clean(capsys.readouterr().err, "usage error")


# --------------------------------------------------------------------------- the review of 2026-09-27


def test_a_check_longer_than_one_check_takes_is_refused(vp, daemon, monkeypatch):
    """sec-vault-1: one request no longer confirms hundreds of thousands of guessed names at once."""
    monkeypatch.setattr(vault, "MAX_CHECK_CHARS", 1000)
    answer, wire = raw(vp.check_socket, {"op": "check", "text": "x " * 501})
    assert answer["ok"] is False and answer["error"] == "bad request" and "in pieces" in answer["detail"]
    assert_clean(wire.decode("utf-8"), "the refusal")
    assert raw(vp.check_socket, {"op": "check", "text": "x " * 400})[0]["ok"] is True
    log = (vp.check_log / sorted(f.name for f in vp.check_log.iterdir())[-1]).read_text(encoding="utf-8")
    assert [l.split("\t")[-1] for l in log.splitlines()[-2:]] == ["too-long", "ok"]


def test_the_bytes_of_a_window_count_like_its_requests(vp, monkeypatch):
    """sec-vault-1: the rate limit counts bytes per window as well, so many small requests and one big one
    cost the same."""
    monkeypatch.setattr(vault, "RATE_BYTES", 300)
    with serving(vp):
        big = {"op": "check", "text": "y" * 200}
        assert raw(vp.check_socket, big)[0]["ok"] is True
        assert raw(vp.check_socket, big)[0] == {"ok": False, "error": "rate"}
        assert raw(vp.check_socket, {"op": "ping"})[0]["ok"] is True       # a small request still fits


def test_scans_run_two_at_a_time_and_the_third_waits_then_loses(vp, monkeypatch):
    """sec-vault-2: a scan has a slot budget; when every slot is busy for SCAN_WAIT the request gets rate."""
    monkeypatch.setattr(vault, "MAX_CONCURRENT_SCANS", 1)
    monkeypatch.setattr(vault, "SCAN_WAIT", 0.3)
    gate = threading.Event()
    real = check._scan

    def slow(text, matcher):
        if text == "hold the slot":
            gate.wait(10)
        return real(text, matcher)

    monkeypatch.setattr(check, "_scan", slow)
    with serving(vp):
        t = threading.Thread(target=raw, args=(vp.check_socket, {"op": "check", "text": "hold the slot"}))
        t.start()
        time.sleep(0.15)
        try:
            assert raw(vp.check_socket, {"op": "check", "text": "quick"})[0] == {"ok": False, "error": "rate"}
            assert vault.admin_call("status", sock=vp.admin_sock)["state"] == "plain"   # the admin socket answers
        finally:
            gate.set()
            t.join(10)
        assert raw(vp.check_socket, {"op": "check", "text": "quick"})[0]["ok"] is True


def test_nested_json_is_a_bad_request_and_the_daemon_keeps_serving(vp, daemon):
    """sec-vault-3: a RecursionError from the JSON reader is an answer, not a traceback."""
    deep = b"[" * 200_000 + b"]" * 200_000 + b"\n"
    assert raw(vp.check_socket, deep)[0] == {"ok": False, "error": "bad request"}
    assert vault.ping(vp.check_socket) == "plain"
    assert len(vault_threads()) >= 2


def test_open_file_never_returns_the_register(vp, daemon, monkeypatch):
    """sec-vault-4: the register goes through register_load and register_save only."""
    assert encrypt_vault(monkeypatch) == 0
    answer, wire = raw(vp.admin_sock, {"op": "open_file", "path": str(vp.register_encrypted)})
    assert answer["ok"] is False and answer["error"] == "not allowed" and "register_load" in answer["detail"]
    assert "data" not in answer
    assert_clean(wire.decode("utf-8"), "the refusal")
    with pytest.raises(vault.VaultError):
        vault.admin_call("open_file", path=str(vp.register_encrypted))


def test_show_never_prints_inside_an_assistant_session_to_a_pipe_or_for_the_work_user(monkeypatch, capsys, tmp_path):
    called = []
    monkeypatch.setattr(vault, "admin_call", lambda *a, **k: called.append(a) or {"data": ""})
    f = str(tmp_path / "report.md.gpg")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert vault.main(["show", f]) == 2
    assert "assistant session" in capsys.readouterr().err
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    assert vault.main(["show", f]) == 2              # pytest captures stdout: not a terminal
    assert "terminal only" in capsys.readouterr().err
    monkeypatch.setattr(vault.config, "is_work_user", lambda: True)
    assert vault.main(["show", f]) == 2
    assert called == []


def test_show_prints_the_sealed_file_on_a_terminal(monkeypatch, tmp_path):
    import base64 as _b64
    import io

    class Tty(io.StringIO):
        def isatty(self):
            return True

    out = Tty()
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(vault, "admin_call", lambda op, **k: {"ok": True, "data": _b64.b64encode(b"report text").decode()})
    monkeypatch.setattr(vault.sys, "stdout", out)
    assert vault.main(["show", str(tmp_path / "r.md.gpg")]) == 0
    assert out.getvalue() == "report text\n"
