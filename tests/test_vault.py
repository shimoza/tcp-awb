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


# --------------------------------------------------------------------------- T2: the hand-over of a reload
#
# The daemon before runs in threads of this process (so fast_gpg applies to it), the new one is a real child
# process (`python -I -m awb vault serve --takeover FD`) or a stand-in child where a failure is planted. A fake
# NOTIFY_SOCKET bound here records every datagram in order. No systemd is needed.

STANDIN = r'''
import json, os, sys, time
fd, mode, log = int(sys.argv[1]), sys.argv[2], sys.argv[3]
started = time.monotonic()

def note(**kw):
    with open(log, "a") as fh:
        fh.write(json.dumps(dict(kw, t=time.monotonic())) + "\n")

note(event="start", pid=os.getpid())
from awb import vault
if mode.startswith(("slow:", "late:")):
    wait = float(mode.split(":")[1])
    time.sleep(wait if mode.startswith("slow:") else max(0.0, wait - (time.monotonic() - started)))
    sys.exit(vault.main(["serve", "--takeover", str(fd)]))
if mode == "logged":
    real_send, real_accept = vault._Pair.send, vault.Daemon.accept_now
    def send(self, obj):
        note(event="send", obj=sorted(obj))
        return real_send(self, obj)
    def accept_now(self):
        note(event="go", accepts=sum(self.accepts.values()))
        return real_accept(self)
    vault._Pair.send, vault.Daemon.accept_now = send, accept_now
    sys.exit(vault.main(["serve", "--takeover", str(fd)]))
import socket
pair = vault._Pair(socket.socket(fileno=fd))
if mode == "record":
    fds = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            fds[name] = os.readlink("/proc/self/fd/" + name)
        except OSError:
            pass
    fds = sorted(int(n) for n, target in fds.items() if not target.startswith("/proc/"))
    line = pair.read(time.monotonic() + 10)
    note(event="record", argv=sys.argv, environ=dict(os.environ), fds=fds, line=line)
    pair.send({"ok": False, "error": "failed"})
    sys.exit(1)
line = pair.read(time.monotonic() + 10)
if mode == "failed":
    pair.send({"ok": False, "error": "failed"})
    sys.exit(1)
if mode == "exit":
    sys.exit(1)
if mode == "hang":
    time.sleep(3600)
if mode == "no-serving":
    pair.send({"ok": True, "pid": os.getpid(), "release": vault.release_path()})
    pair.read(time.monotonic() + 10)
    note(event="go")
    time.sleep(3600)
'''


def standin(monkeypatch, tmp: Path, mode: str) -> Path:
    """Replace the child of a reload with the stand-in in `mode`; returns its log file."""
    script, log = tmp / "standin.py", tmp / ("standin-%s.log" % mode.replace(":", "-"))
    script.write_text(STANDIN, encoding="utf-8")
    monkeypatch.setattr(vault, "_takeover_command",
                        lambda fd: [sys.executable, "-I", str(script), str(fd), mode, str(log)])
    return log


def notes(log: Path) -> list[dict]:
    try:
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    except FileNotFoundError:
        return []


def gone(pid: int, wait: float = 20.0) -> bool:
    """True once the process has no /proc entry (exited and reaped: no zombie)."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if not Path("/proc/%d" % pid).exists():
            return True
        time.sleep(0.05)
    return False


def stop_child(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    assert gone(pid), "the new daemon did not stop"


class Notify:
    """A fake NOTIFY_SOCKET: every datagram with its arrival time, in order."""

    def __init__(self, path: Path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        self.sock.settimeout(0.2)
        self.got: list[tuple[float, str]] = []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.done.is_set():
            try:
                data = self.sock.recv(4096)
            except OSError:
                continue
            self.got.append((time.monotonic(), data.decode()))

    def messages(self) -> list[str]:
        return [m for _, m in self.got]

    def close(self):
        self.done.set()
        self.thread.join(timeout=2)
        self.sock.close()


@pytest.fixture
def notify(sockets, monkeypatch):
    n = Notify(sockets / "notify")
    monkeypatch.setenv("NOTIFY_SOCKET", str(sockets / "notify"))
    try:
        yield n
    finally:
        n.close()


@contextlib.contextmanager
def parent(vp: config.Paths):
    """The daemon before, serving in a thread of this process the way `awb vault serve` does. At the end the new
    daemon a reload started is stopped too."""
    d = vault.Daemon(vp)
    d.start()
    t = threading.Thread(target=d.serve_forever, daemon=True)
    t.start()
    d.thread, d.children = t, []
    try:
        yield d
    finally:
        if not d.handed_over.is_set():
            d.stopping.set()
        t.join(timeout=30)
        assert not t.is_alive()
        for pid in d.children:
            stop_child(pid)


def reload(d, timeout: float | None = None) -> dict:
    answer = vault.admin_call("reload", timeout=vault.RELOAD_CLIENT_TIMEOUT if timeout is None else timeout)
    d.children.append(answer["pid"])
    return answer


def inodes(vp: config.Paths) -> list[tuple[int, int]]:
    return [(os.lstat(f).st_dev, os.lstat(f).st_ino) for f in (vp.admin_sock, vp.check_socket)]


def expected_hits() -> list[dict]:
    return [{"start": h["start"], "length": h["length"], "cls": h["cls"]} for h in check._scan(
        TEXT, check.Matcher(register.forms_for_matching(register.parse("\n".join(fixtures.register_lines()) + "\n"))))]


def test_a_reload_keeps_the_state_unlocked_and_the_socket_files(vp, notify, monkeypatch):
    """Planted: a client pinging and checking in a loop with a 2 s bound across a real reload sees locked, a refused
    connection, a timeout or a missing socket; a socket file changes its inode; status differs after the reload
    except for pid and release."""
    with parent(vp) as d:
        assert encrypt_vault(monkeypatch) == 0 and d.state() == "unlocked"
        want = vault.check_remote(TEXT, vp.check_socket)
        assert want and want == expected_hits()
        before, files = vault.admin_call("status"), inodes(vp)
        seen: list[str] = []
        stop = threading.Event()

        def probe():
            while not stop.is_set():
                try:
                    state = vault.ping(vp.check_socket, timeout=2.0)
                    hits = vault.check_remote(TEXT, vp.check_socket, timeout=2.0)
                    seen.append("ok" if state == "unlocked" and hits == want else "wrong %s" % state)
                except vault.VaultError as err:
                    seen.append(type(err).__name__)
                if not (vp.admin_sock.exists() and vp.check_socket.exists()):
                    seen.append("missing")

        prober = threading.Thread(target=probe)
        prober.start()
        time.sleep(0.3)
        answer = reload(d)
        d.thread.join(timeout=30)
        assert not d.thread.is_alive(), "the daemon before did not leave"
        time.sleep(0.5)
        stop.set()
        prober.join(timeout=10)
        assert answer["state"] == "unlocked" and answer["pid"] != os.getpid()
        assert answer["release"] == vault.release_path()
        assert set(seen) == {"ok"} and len(seen) > 5, sorted(set(seen))
        assert inodes(vp) == files
        after = vault.admin_call("status")
        assert after["pid"] == answer["pid"]
        assert {k: v for k, v in after.items() if k not in ("pid", "release")} == \
            {k: v for k, v in before.items() if k not in ("pid", "release")}
        assert vault.check_remote(TEXT, vp.check_socket) == want
        assert "MAINPID=%d" % answer["pid"] in notify.messages()


def test_checks_and_pings_answer_during_the_hand_over(vp, notify, monkeypatch, tmp_path):
    """Planted: an _op_reload that keeps the daemon lock across the wait for the child makes both time out."""
    standin(monkeypatch, tmp_path, "slow:5")
    with parent(vp) as d:
        assert encrypt_vault(monkeypatch) == 0
        result = {}
        t = threading.Thread(target=lambda: result.update(reload(d)))
        t.start()
        assert d.handing_over.wait(10)
        time.sleep(0.5)
        started = time.monotonic()
        assert vault.ping(vp.check_socket, timeout=1.0) == "unlocked"
        assert vault.check_remote(TEXT, vp.check_socket, timeout=1.0) == expected_hits()
        assert vault.admin_call("status", timeout=1.0)["state"] == "unlocked"
        assert time.monotonic() - started < 2.0
        assert d.handing_over.is_set() and not d.handed_over.is_set(), "the child answered too early for the test"
        t.join(timeout=60)
        assert result["state"] == "unlocked"


def test_the_passphrase_travels_on_the_socket_pair_only(vp, notify, monkeypatch, tmp_path):
    """Planted: the passphrase in the child's argv or environment; a descriptor beyond 0, 1, 2 and the three passed
    ones (the pair and the two listening sockets)."""
    log = standin(monkeypatch, tmp_path, "record")
    with parent(vp) as d:
        assert encrypt_vault(monkeypatch) == 0
        with pytest.raises(vault.VaultError, match="the new daemon did not come up"):
            vault.admin_call("reload")
        assert vault.ping(vp.check_socket) == "unlocked"
        (rec,) = [n for n in notes(log) if n["event"] == "record"]
        line = rec["line"]
        assert line["passphrase"] == PASS and line["state"] == "unlocked"
        assert PASS not in " ".join(rec["argv"])
        assert not [k for k, v in rec["environ"].items() if PASS in k or PASS in v]
        pair_fd = int(rec["argv"][1])
        assert rec["fds"] == sorted({0, 1, 2, pair_fd, line["admin_fd"], line["check_fd"]})
        assert line["admin_path"] == str(vp.admin_sock) and line["check_path"] == str(vp.check_socket)


@pytest.mark.parametrize("mode", ["failed", "exit", "hang", "no-serving"])
def test_a_child_that_fails_leaves_the_old_daemon_serving(vp, notify, monkeypatch, tmp_path, mode):
    """Planted: the daemon before stops answering or accepting, is locked, sent MAINPID, lost a socket file, answers
    anything but the fixed detail, or leaves its child alive or a zombie."""
    monkeypatch.setattr(vault, "HANDOVER_DEADLINE", 3.0)
    log = standin(monkeypatch, tmp_path, mode)
    with parent(vp) as d:
        assert encrypt_vault(monkeypatch) == 0
        files = inodes(vp)
        with pytest.raises(vault.VaultError) as err:
            vault.admin_call("reload")
        assert str(err.value).startswith("failed: the new daemon did not come up (")
        assert vault.ping(vp.check_socket) == "unlocked"
        assert vault.admin_call("status")["pid"] == os.getpid()
        assert vault.check_remote(TEXT, vp.check_socket) == expected_hits()
        assert inodes(vp) == files
        assert not [m for m in notify.messages() if m.startswith("MAINPID")]
        (start,) = [n for n in notes(log) if n["event"] == "start"]
        assert gone(start["pid"], 2.0), "the child lives on or is a zombie"
        assert not d.handing_over.is_set() and not d.handed_over.is_set()
        assert vault.admin_call("register_load")["ok"]


def test_the_order_is_go_serving_mainpid(vp, notify, monkeypatch, tmp_path):
    """Planted: MAINPID before serving, READY=1 before MAINPID, or the child accepting before go (its accept counter
    must be zero until then)."""
    log = standin(monkeypatch, tmp_path, "logged")
    with parent(vp) as d:
        assert encrypt_vault(monkeypatch) == 0
        answer = reload(d)
        d.thread.join(timeout=30)
        time.sleep(0.5)
        events = notes(log)
        (go,) = [n for n in events if n["event"] == "go"]
        assert go["accepts"] == 0
        sends = [n for n in events if n["event"] == "send"]
        assert [s["obj"] for s in sends] == [["ok", "pid", "release"], ["serving"]]
        serving = sends[1]["t"]
        got = [(t, m) for t, m in notify.got]
        assert [m for _, m in got] == ["MAINPID=%d" % answer["pid"], "READY=1"]
        assert serving < got[0][0] < got[1][0]
        assert sends[0]["t"] < go["t"] < serving


def test_a_write_between_ok_and_go_is_answered_busy_by_the_parent(vp, notify, monkeypatch):
    """Planted: a register_save sent after the child's ok and before go is accepted by anyone; the new daemon's
    matcher differs from the register file after the hand-over."""
    with parent(vp) as d:
        assert encrypt_vault(monkeypatch) == 0
        text = vault.admin_call("register_load")["text"]
        planted = text + "\t".join((fixtures.PARTNER_CODE + "-PERS-1", "PERS", fixtures.PLANTED_PERSON,
                                    fixtures.TODAY, "active")) + "\n"
        saved = vp.register_encrypted.read_bytes()
        caught: list = []
        real_pause = d._pause

        def pause(*a, **k):
            try:
                vault.admin_call("register_save", text=planted)
                caught.append("accepted")
            except vault.VaultBusy:
                caught.append("busy")
            return real_pause(*a, **k)

        d._pause = pause
        reload(d)
        d.thread.join(timeout=30)
        assert caught == ["busy"]
        assert vp.register_encrypted.read_bytes() == saved
        assert vault.check_remote("met %s today" % fixtures.PLANTED_PERSON, vp.check_socket) == []
        assert vault.check_remote(TEXT, vp.check_socket) == expected_hits()
        assert vault.admin_call("register_load")["text"] == text


def test_write_ops_are_busy_during_the_hand_over_and_the_order_is_total(vp, notify, monkeypatch, tmp_path):
    """Planted: a writer that passed the flag check before taking the daemon lock and runs after the copy (the flag
    checked before the lock); a write op that is not busy during the hand-over; a new daemon that refuses
    register_save afterwards."""
    standin(monkeypatch, tmp_path, "slow:4")
    with parent(vp) as d:
        assert encrypt_vault(monkeypatch) == 0
        text = vault.admin_call("register_load")["text"]
        planted = text + "\t".join((fixtures.PARTNER_CODE + "-PERS-1", "PERS", fixtures.PLANTED_PERSON,
                                    fixtures.TODAY, "active")) + "\n"
        early: list = []
        result: dict = {}

        def save():
            try:
                vault.admin_call("register_save", text=planted)
                early.append("accepted")
            except vault.VaultBusy:
                early.append("busy")

        with d._lock:     # a writer queued on the daemon lock before the reload took its copy
            writer = threading.Thread(target=save)
            writer.start()
            time.sleep(0.3)
            t = threading.Thread(target=lambda: result.update(reload(d)))
            t.start()
            assert d.handing_over.wait(10)
            time.sleep(0.2)
        writer.join(timeout=30)
        assert early == ["busy"]
        f = vp.originals / "F-AB2C.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n", encoding="utf-8")
        for op, fields in (("register_save", {"text": planted}), ("unlock", {"passphrase": PASS}),
                           ("lock", {}), ("seal_file", {"path": str(f)})):
            with pytest.raises(vault.VaultBusy, match="reloading, try again"):
                vault.admin_call(op, **fields)
        assert not d.handed_over.is_set(), "the child answered too early for the test"
        t.join(timeout=60)
        assert result["state"] == "unlocked"
        d.thread.join(timeout=30)
        assert vault.admin_call("register_save", text=planted)["ok"]
        assert vault.check_remote("met %s today" % fixtures.PLANTED_PERSON, vp.check_socket)


def served(tmp: Path) -> tuple[subprocess.Popen, Path]:
    """`awb vault serve` as a process of its own. Its output goes to a file: the new daemon of a reload inherits it,
    so a pipe would stay open after the daemon before has left."""
    log = tmp / "serve.log"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    with open(log, "wb") as out:
        proc = subprocess.Popen([sys.executable, "-m", "awb", "vault", "serve"], env=env, cwd=str(ROOT),
                                stdout=out, stderr=out)
    return proc, log


def test_the_parent_leaves_through_the_hand_over_exit(vp, tmp_path):
    """Planted: an exit through stop() (the socket files go), a connection of before the switch cut off in its
    request, a SIGTERM to the handed-over daemon that removes the new daemon's files."""
    proc, log = served(tmp_path)
    child = None
    try:
        deadline = time.monotonic() + 20
        while not (vp.check_socket.exists() and vp.admin_sock.exists()):
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        files = inodes(vp)
        half = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        half.settimeout(15)
        half.connect(str(vp.check_socket))
        line = json.dumps({"op": "check", "text": TEXT}).encode()
        half.sendall(line[:10])           # a request in flight across the switch
        time.sleep(0.3)
        answer = vault.admin_call("reload")
        child = answer["pid"]
        assert child != proc.pid
        time.sleep(0.5)
        assert proc.poll() is None, "the daemon before left with a request in flight"
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        half.sendall(line[10:] + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = half.recv(65536)
            assert chunk, "the request in flight was cut off"
            buf += chunk
        half.close()
        assert json.loads(buf)["hits"] == expected_hits()
        assert proc.wait(timeout=20) == 0 and "handed over" in log.read_text()
        assert inodes(vp) == files
        assert vault.ping(vp.check_socket) == "plain" and vault.admin_call("status")["pid"] == child
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if child:
            stop_child(child)
    assert not vp.check_socket.exists() and not vp.admin_sock.exists(), "the new daemon's clean stop"


def test_a_reload_from_another_uid_is_refused(vp, daemon, monkeypatch):
    def never(fd):
        raise AssertionError("a child was started")

    monkeypatch.setattr(vault, "_takeover_command", never)
    monkeypatch.setattr(vault, "_peer_uid", lambda conn: os.getuid() + 1)
    assert raw(vp.admin_sock, {"op": "reload"})[0] == {"ok": False, "error": "refused"}
    with pytest.raises(vault.VaultUnavailable):
        vault.admin_call("reload")
    assert vault.main(["reload"]) == 2
    assert not daemon.handing_over.is_set()


def test_the_reload_client_outlasts_the_parent(vp, notify, monkeypatch, tmp_path):
    """The clock is scaled: the daemon's deadline and the client's timeout shrink by the same factor. The stand-in
    child answers just before the daemon's deadline: the client must still get the ok. Planted: a client timeout
    equal to the daemon's wait."""
    assert vault.RELOAD_CLIENT_TIMEOUT - vault.HANDOVER_DEADLINE >= 20
    scale = 6.0 / vault.HANDOVER_DEADLINE
    monkeypatch.setattr(vault, "HANDOVER_DEADLINE", vault.HANDOVER_DEADLINE * scale)
    monkeypatch.setattr(vault, "RELOAD_LOCK_WAIT", 1.0)
    standin(monkeypatch, tmp_path, "late:5.0")
    with parent(vp) as d:
        assert encrypt_vault(monkeypatch) == 0
        started = time.monotonic()
        answer = reload(d, timeout=vault.RELOAD_CLIENT_TIMEOUT * scale)
        assert answer["state"] == "unlocked" and time.monotonic() - started > 4.5


def test_a_reload_waits_for_an_intake_and_an_intake_waits_for_a_reload(vp, notify, monkeypatch, tmp_path):
    """Planted: a reload that starts its child while an intake holds the vault; an intake that runs during a
    hand-over."""
    log = standin(monkeypatch, tmp_path, "slow:3")
    with parent(vp) as d:
        assert encrypt_vault(monkeypatch) == 0
        result: dict = {}
        with vault.vault_lock(vp):
            t = threading.Thread(target=lambda: result.update(reload(d)))
            t.start()
            time.sleep(1.0)
            assert not notes(log) and not result
            released = time.monotonic()
        time.sleep(1.0)
        (start,) = [n for n in notes(log) if n["event"] == "start"]
        assert start["t"] > released
        with pytest.raises(vault.VaultError, match="holds the vault"):
            with vault.vault_lock(vp, wait=0.5):
                pass
        t.join(timeout=60)
        assert result["state"] == "unlocked"


def test_ready_is_sent_at_a_normal_start_and_nothing_without_the_variable(vp, notify, monkeypatch):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([sys.executable, "-m", "awb", "vault", "serve"], env=env, cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while "READY=1" not in notify.messages():
            assert proc.poll() is None and time.monotonic() < deadline, "no READY=1"
            time.sleep(0.05)
        assert vp.admin_sock.exists() and vp.check_socket.exists()
        assert notify.messages() == ["READY=1"]
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=20)
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert vault.sd_notify("READY=1") is False
    time.sleep(0.3)
    assert notify.messages() == ["READY=1"]


def test_a_locked_daemon_reloads_and_stays_locked_with_its_time(vp, notify, monkeypatch, tmp_path):
    """Planted: a line that carries a passphrase while locked; a lock time lost in the hand-over."""
    real_command = vault._takeover_command
    with parent(vp) as d:
        assert encrypt_vault(monkeypatch) == 0
        assert vault.admin_call("lock")["state"] == "locked"
        since = vault.admin_call("status")["since"]
        assert SINCE_RE.fullmatch(since)
        log = standin(monkeypatch, tmp_path, "record")
        with pytest.raises(vault.VaultError):
            vault.admin_call("reload")
        (rec,) = [n for n in notes(log) if n["event"] == "record"]
        assert "passphrase" not in rec["line"] and rec["line"]["state"] == "locked"
        assert rec["line"]["since"] == since
        monkeypatch.setattr(vault, "_takeover_command", real_command)
        time.sleep(1.1)
        answer = reload(d)
        assert answer["state"] == "locked"
        d.thread.join(timeout=30)
        status = vault.admin_call("status")
        assert status["pid"] == answer["pid"] and status["state"] == "locked" and status["since"] == since
        assert vault.ping_state(vp.check_socket) == {"state": "locked", "since": since}


def test_an_inherited_socket_is_served_with_the_timeout_and_the_identity(vp):
    """Planted: a skipped settimeout (the inherited description is non-blocking: accept spins every 50 ms, 20 times a
    second) or an identity that is not the file's lstat."""
    srv, _ = vault._bind(vp.admin_sock, 0o600, None)
    chk, _ = vault._bind(vp.check_socket, 0o660, 0o750)
    # as the child gets them: a socket object over a description another process left non-blocking
    inherited = {True: socket.socket(fileno=os.dup(srv.fileno())), False: socket.socket(fileno=os.dup(chk.fileno()))}
    srv.close()
    chk.close()
    assert inherited[True].gettimeout() is None
    d = vault.Daemon(vp)
    d.start(inherited=inherited, accept=False)
    try:
        for s, path in ((inherited[True], vp.admin_sock), (inherited[False], vp.check_socket)):
            assert s.gettimeout() == 0.2
        st = os.lstat(vp.admin_sock)
        assert d._servers[0][2] == (st.st_dev, st.st_ino)
        time.sleep(0.5)
        assert d.accepts == {"admin": 0, "check": 0}
        d.accept_now()
        time.sleep(1.0)
        assert 0 < d.accepts["admin"] < 10 and 0 < d.accepts["check"] < 10, d.accepts
        assert vault.ping(vp.check_socket) == "plain"
    finally:
        d.stop()
    assert not vp.admin_sock.exists() and not vp.check_socket.exists()


def test_the_daemons_are_not_dumpable(vp, tmp_path):
    """Planted: prctl(PR_GET_DUMPABLE) of the served process is 1: /proc/<pid> stays the owner's and readable."""
    proc, _ = served(tmp_path)
    child = None
    try:
        deadline = time.monotonic() + 20
        while not (vp.check_socket.exists() and vp.admin_sock.exists()):
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        for pid in (proc.pid,):
            assert os.stat("/proc/%d/status" % pid).st_uid == 0
            with pytest.raises(PermissionError):
                Path("/proc/%d/environ" % pid).read_bytes()
        child = vault.admin_call("reload")["pid"]
        assert os.stat("/proc/%d/status" % child).st_uid == 0, "the new daemon sets the flag again after exec"
        assert os.stat("/proc/%d/status" % os.getpid()).st_uid == os.getuid()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
        if child:
            stop_child(child)


def test_status_and_ping_answer_ops_and_release(vp, daemon):
    """Planted: an answer without ops, without reload in it, or a release that is not the real path of the tree of
    the package; ops or release on the check socket."""
    import awb

    tree = os.path.dirname(os.path.realpath(awb.__file__))
    assert vault.release_path() == os.path.dirname(tree)
    for op in ("status", "ping"):
        answer = vault.admin_call(op)
        assert "reload" in answer["ops"] and answer["ops"] == sorted(vault.Daemon._ADMIN_OPS)
        assert answer["release"] == os.path.dirname(tree)
    assert set(raw(vp.check_socket, {"op": "ping"})[0]) == {"ok", "state"}


def test_the_module_text_promise_still_holds():
    """Planted: a passphrase, the tenants or the digest in a print, a log line, an argv or an environment."""
    import ast

    banned = {"pw", "passphrase", "_passphrase", "tenants", "digest", "line", "secrets"}
    for rel in ("awb/vault.py", "awb/tcp/keys.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            reach = []
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                if name in ("print", "_log", "Popen", "_takeover_command", "putenv", "sd_notify") or (
                        name == "write" and re.search(r"std(err|out)", ast.dump(f))):
                    reach = node.args + [k.value for k in node.keywords if k.arg not in ("pass_fds", "input")]
            elif isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Subscript) and "environ" in ast.dump(t.value) for t in node.targets):
                reach = [node.value]
            elif isinstance(node, ast.FunctionDef) and node.name in ("_takeover_command",):
                reach = [r.value for r in ast.walk(node) if isinstance(r, ast.Return)]
            for r in reach:
                for n in ast.walk(r):
                    word = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
                    assert word not in banned, "%s:%d carries %s" % (rel, node.lineno, word)
