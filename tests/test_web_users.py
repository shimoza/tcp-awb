"""Per-user logins and the lockout of the gateway (F4): the users file of `awb web user`, the wait per login, the
site-wide cap, the per-address limit, the sign-in log and `awb web status`. A temporary users file and a fake clock;
the logins are invented."""
import argparse
import hashlib
import json
import re
import threading
import http.client
from pathlib import Path
from urllib.parse import urlencode

import pytest

from awb.tcp.web import gateway, publish, users

DOMAIN = "awb.example.test"
ROUNDS = 1000
T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


def write_legacy(path, login="awb", password="legacy-test-pw"):
    salt = b"test-only-salt"
    path.write_text(json.dumps({"username": login, "salt": salt.hex(), "rounds": ROUNDS,
                                "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ROUNDS).hex()}))


@pytest.fixture
def site(tmp_path):
    """A legacy auth.json, a users file with two invented logins, a log and a fake clock."""
    write_legacy(tmp_path / "auth.json")
    path = tmp_path / "users.json"
    pw = {login: users.change(path, "add", login, False, rounds=ROUNDS) for login in ("reader1", "reader2")}
    clock = Clock()
    auth = gateway.Auth(tmp_path / "auth.json", path, tmp_path / "signin.log", clock=clock)
    return auth, pw, clock, tmp_path


def log_entries(tmp_path):
    return [json.loads(line) for line in (tmp_path / "signin.log").read_text().splitlines()]


# --------------------------------------------------------------------------- 1) the users file


def test_add_stores_a_salted_digest_and_never_the_password(tmp_path):
    path = tmp_path / "users.json"
    a = users.change(path, "add", "reader1", False, rounds=ROUNDS)
    b = users.change(path, "add", "reader2", False, rounds=ROUNDS)
    assert re.fullmatch(r"[A-Za-z0-9]{20}", a) and re.fullmatch(r"[A-Za-z0-9]{20}", b) and a != b
    text = path.read_text()
    assert a not in text and b not in text
    data = json.loads(text)["users"]
    assert data["reader1"]["salt"] != data["reader2"]["salt"]
    assert data["reader1"]["hash"] == hashlib.pbkdf2_hmac("sha256", a.encode(), bytes.fromhex(data["reader1"]["salt"]),
                                                      ROUNDS).hex()
    assert path.stat().st_mode & 0o777 == 0o640
    with pytest.raises(users.Refused, match="exists"):
        users.change(path, "add", "reader1", False, rounds=ROUNDS)
    with pytest.raises(users.Refused, match="no such login"):
        users.change(path, "reset", "reader9", False, rounds=ROUNDS)
    with pytest.raises(users.Refused, match="a login is"):
        users.change(path, "add", "has space", False, rounds=ROUNDS)


def test_the_single_login_works_until_the_first_user_is_added(tmp_path):
    write_legacy(tmp_path / "auth.json")
    path = tmp_path / "users.json"
    auth = gateway.Auth(tmp_path / "auth.json", path, clock=Clock())
    assert auth.sign_in("awb", "legacy-test-pw", "a1") == "ok"
    old = auth.new_session("awb")
    assert auth.session_valid(old)
    pw = users.change(path, "add", "reader1", False, rounds=ROUNDS)
    assert auth.sign_in("awb", "legacy-test-pw", "a1") == "fail"
    assert not auth.session_valid(old)
    assert auth.sign_in("reader1", pw, "a1") == "ok"
    users.change(path, "remove", "reader1", False)
    assert json.loads(path.read_text())["users"] == {}
    assert auth.sign_in("awb", "legacy-test-pw", "a1") == "fail"


def test_reset_and_remove_end_the_sessions_of_that_login(site):
    auth, pw, clock, tmp_path = site
    path = tmp_path / "users.json"
    assert auth.sign_in("reader1", pw["reader1"], "a1") == "ok"
    reader1, reader2 = auth.new_session("reader1"), auth.new_session("reader2")
    new = users.change(path, "reset", "reader1", False, rounds=ROUNDS)
    assert not auth.session_valid(reader1) and auth.session_valid(reader2)
    assert auth.sign_in("reader1", pw["reader1"], "a1") == "fail"
    assert auth.sign_in("reader1", new, "a1") == "ok"
    users.change(path, "remove", "reader2", False)
    assert not auth.session_valid(reader2)
    assert auth.sign_in("reader2", pw["reader2"], "a2") == "fail"


def test_the_command_prints_the_password_once_and_refuses_in_an_assistant_session(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    env = {"AWB_WEB_USERS": str(path)}
    out = []
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    a = argparse.Namespace(action="add", login="reader1")
    assert users.main(a, env, lambda: 1000, out.append) == 0
    password = re.search(r"Password \(shown once, not stored\): ([A-Za-z0-9]{20})$", out[0]).group(1)
    assert password not in path.read_text()
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(users.Refused, match="assistant session"):
        users.main(argparse.Namespace(action="reset", login="reader1"), env, lambda: 1000, out.append)
    assert len(out) == 1
    monkeypatch.delenv("CLAUDECODE")
    with pytest.raises(users.Refused, match="sudo"):
        users.main(a, {}, lambda: 1000, out.append)


# --------------------------------------------------------------------------- 2) the wait per login


def test_five_failures_within_15_minutes_make_the_login_wait_15_minutes(site):
    auth, pw, clock, _ = site
    for i in range(5):
        assert auth.sign_in("reader1", "wrong", "a%d" % i) == "fail"
        clock.tick(170)
    assert auth.sign_in("reader1", pw["reader1"], "a9") == "cooldown"
    assert auth.sign_in("reader2", pw["reader2"], "a9") == "ok"
    clock.tick(15 * 60 - 170)
    assert auth.sign_in("reader1", pw["reader1"], "a9") == "ok"


def test_failures_spread_over_more_than_15_minutes_do_not_lock(site):
    auth, pw, clock, _ = site
    for i in range(8):
        assert auth.sign_in("reader1", "wrong", "a%d" % i) == "fail"
        clock.tick(4 * 60)
    assert auth.sign_in("reader1", pw["reader1"], "a9") == "ok"


def test_each_further_series_doubles_the_wait_up_to_24_hours_and_a_success_resets(site):
    auth, pw, clock, _ = site
    waits = []
    for series in range(9):
        for i in range(5):
            assert auth.sign_in("reader1", "wrong", "s%d-%d" % (series, i)) == "fail"
        until = auth.limiter.waits_until("reader1", clock())
        waits.append(until - clock())
        clock.now = until
    assert waits == [900, 1800, 3600, 7200, 14400, 28800, 57600, 86400, 86400]
    assert auth.sign_in("reader1", pw["reader1"], "z") == "ok"
    for i in range(5):
        auth.sign_in("reader1", "wrong", "r%d" % i)
    assert auth.limiter.waits_until("reader1", clock()) - clock() == 900


def test_an_unknown_login_is_counted_and_answered_like_a_wrong_password(site):
    auth, pw, clock, tmp_path = site
    server = gateway.make_server(0, auth, tmp_path / "auth.json", 1, 1, domain=DOMAIN)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def attempt(login, password, address):
        c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        c.request("GET", "/login", headers={"Host": DOMAIN})
        r = c.getresponse()
        page, cookie = r.read(), r.getheader("Set-Cookie").split(";")[0]
        c.close()
        csrf = re.search(rb'name="csrf" value="([^"]+)"', page).group(1).decode()
        body = urlencode({"csrf": csrf, "username": login, "password": password, "next": "/"})
        c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        c.request("POST", "/login", body=body, headers={
            "Host": DOMAIN, "Cookie": cookie, "Origin": "https://" + DOMAIN, "CF-Connecting-IP": address,
            "Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        out = (r.status, re.sub(r"=[^;]*;", "=;", r.getheader("Set-Cookie") or "", count=1),
               re.sub(rb'value="[^"]*"', b"", r.read()))
        c.close()
        return out

    try:
        for i in range(5):
            known = attempt("reader1", "wrong", "k%d" % i)
            unknown = attempt("reader9", "wrong", "u%d" % i)
            assert known == unknown
            assert known[0] == 200 and b"The username or password is incorrect." in known[2]
        known, unknown = attempt("reader1", pw["reader1"], "k9"), attempt("reader9", "wrong", "u9")
        assert known == unknown
        assert known[0] == 429 and gateway.COOLDOWN_TEXT.encode() in known[2]
        assert attempt("reader2", pw["reader2"], "l1")[0] == 303
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- 3) the site-wide cap


def test_30_failures_an_hour_close_every_sign_in_until_the_hour_passes(site):
    auth, pw, clock, _ = site
    for i in range(30):
        assert auth.sign_in("x%02d" % i, "wrong", "a%d" % i) == "fail"
        clock.tick(60)
    assert auth.sign_in("reader2", pw["reader2"], "fresh") == "cooldown"
    assert auth.sign_in("reader1", pw["reader1"], "fresh2") == "cooldown"
    clock.now = T0 + 3600
    assert auth.sign_in("reader2", pw["reader2"], "fresh") == "ok"


# --------------------------------------------------------------------------- 4) the per-address limit stays


def test_the_per_address_limit_stays_eight_a_minute(site):
    auth, pw, clock, _ = site
    for i in range(8):
        assert auth.sign_in("y%d" % i, "wrong", "one-address") == "fail"
    assert auth.sign_in("reader2", pw["reader2"], "one-address") == "limited"
    assert auth.sign_in("reader2", pw["reader2"], "other-address") == "ok"
    clock.tick(60)
    assert auth.sign_in("reader2", pw["reader2"], "one-address") == "ok"


# --------------------------------------------------------------------------- 5) the log and awb web status


def test_every_attempt_is_logged_with_time_login_and_result_never_the_password(site):
    auth, pw, clock, tmp_path = site
    auth.sign_in("reader1", pw["reader1"], "a1")
    for i in range(5):
        auth.sign_in("reader2", "wrong-secret-%d" % i, "b%d" % i)
    auth.sign_in("reader2", pw["reader2"], "c1")
    auth.sign_in("not a login!", "wrong", "d1")
    entries = log_entries(tmp_path)
    assert [(e["login"], e["result"]) for e in entries] == [("reader1", "ok")] + [("reader2", "fail")] * 5 + [
        ("reader2", "cooldown"), (gateway.NOT_A_LOGIN, "fail")]
    assert all(set(e) == {"time", "login", "result"} for e in entries)
    assert entries[0]["time"] == gateway.log_time(T0)
    text = (tmp_path / "signin.log").read_text()
    assert pw["reader1"] not in text and pw["reader2"] not in text and "wrong-secret" not in text


def test_a_restarted_gateway_keeps_the_waits_from_the_log(site):
    auth, pw, clock, tmp_path = site
    for i in range(5):
        auth.sign_in("reader1", "wrong", "a%d" % i)
    again = gateway.Auth(tmp_path / "auth.json", tmp_path / "users.json", tmp_path / "signin.log", clock=clock)
    assert again.sign_in("reader1", pw["reader1"], "b") == "cooldown"


def test_status_shows_the_counts_of_the_last_day_and_the_logins_in_cooldown(site):
    auth, pw, clock, tmp_path = site
    auth.sign_in("reader1", pw["reader1"], "old")
    clock.tick(25 * 3600)
    auth.sign_in("reader1", pw["reader1"], "a1")
    for i in range(5):
        auth.sign_in("reader2", "wrong", "b%d" % i)
    auth.sign_in("reader2", "wrong", "b9")
    lines = publish.signin_lines(tmp_path / "signin.log", clock())
    assert lines[0] == "sign-ins, last 24 h: 7 (cooldown 1, fail 5, ok 1)"
    assert lines[1] == "logins in cooldown: reader2 until %s" % gateway.log_time(clock() + 900)
    for i in range(30):
        auth.sign_in("z%02d" % i, "wrong", "c%d" % i)
    lines = publish.signin_lines(tmp_path / "signin.log", clock())
    assert any(line.startswith("site closed: 30 failures within the hour") for line in lines)
    assert publish.signin_lines(tmp_path / "none.log", clock()) == ["sign-ins: no log yet"]


# --------------------------------------------------------------------------- 6) T9 step 1: levels, generation, TOTP


def test_an_owner_entry_gets_a_totp_secret_and_a_reset_raises_the_generation(tmp_path):
    path = tmp_path / "users.json"
    users.change(path, "add", "reader1", False, rounds=ROUNDS)
    users.change(path, "add", "owner1", False, rounds=ROUNDS, level="owner")
    data = json.loads(path.read_text())["users"]
    assert data["reader1"]["level"] == "reader" and "totp" not in data["reader1"]
    assert data["owner1"]["level"] == "owner" and re.fullmatch(r"[A-Z2-7]{32}", data["owner1"]["totp"])
    assert data["owner1"]["generation"] == 1
    secret = data["owner1"]["totp"]
    users.change(path, "reset", "owner1", False, rounds=ROUNDS)
    again = json.loads(path.read_text())["users"]["owner1"]
    assert again["generation"] == 2 and again["level"] == "owner" and again["totp"] != secret
    with pytest.raises(users.Refused, match="--level"):
        users.change(path, "reset", "owner1", False, rounds=ROUNDS, level="reader")
    with pytest.raises(users.Refused, match="--level"):
        users.change(path, "add", "x1", False, rounds=ROUNDS, level="admin")
    auth = gateway.Auth(tmp_path / "missing-auth.json", path)
    assert auth.accounts()[1]["owner1"].totp == again["totp"] and auth.accounts()[1]["reader1"].totp is None


def test_the_owner_enrolment_is_printed_once_with_the_key_and_the_otpauth_line(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    out = []
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(users.shutil, "which", lambda name: None)
    a = argparse.Namespace(action="add", login="owner1", level="owner")
    assert users.main(a, {"AWB_WEB_USERS": str(path)}, lambda: 1000, out.append) == 0
    secret = json.loads(path.read_text())["users"]["owner1"]["totp"]
    assert out[1].endswith(" ".join(secret[i:i + 4] for i in range(0, 32, 4)))
    assert out[2] == users.enrolment("owner1", secret)
    assert out[2].startswith("otpauth://totp/AWB:owner1?secret=%s&issuer=AWB" % secret)
    assert "passphrase" in out[-1]
    out.clear()
    users.main(argparse.Namespace(action="list", login=None), {"AWB_WEB_USERS": str(path)}, lambda: 1000, out.append)
    assert out == ["logins: owner1 (owner)"]


def test_the_qr_code_comes_from_qrencode_on_standard_input_only(monkeypatch):
    seen = {}

    def runner(cmd, **kw):
        seen.update(cmd=cmd, input=kw.get("input"))
        return argparse.Namespace(returncode=0, stdout="██ █\n█ ██\n".encode())

    monkeypatch.setattr(users.shutil, "which", lambda name: "/usr/bin/qrencode")
    assert users.qr_lines("otpauth://totp/x", runner) == ["██ █", "█ ██"]
    assert seen["cmd"] == ["/usr/bin/qrencode", "-t", "ANSIUTF8", "-o", "-"] and seen["input"] == b"otpauth://totp/x"
    monkeypatch.setattr(users.shutil, "which", lambda name: None)
    assert users.qr_lines("otpauth://totp/x", runner) == []


def test_awb_web_host_writes_the_owner_host_once(tmp_path, monkeypatch, capsys):
    conf = tmp_path / "paths.conf"
    conf.write_text("owner = ownr\nowner_host = old.example.test\nwork_user = awb\n")
    host = publish.Host(geteuid=lambda: 1000, environ={"AWB_CONF": str(conf)})
    monkeypatch.setattr(publish, "GATEWAY_UNIT", tmp_path / "none.service")
    assert publish.main(["host", "Owner.Example.Test"], host) == 0
    assert conf.read_text() == "owner = ownr\nwork_user = awb\nowner_host = owner.example.test\n"
    assert publish.main(["host", "https://x.example.test"], host) == 1
    unit = tmp_path / "awb-web.service"
    unit.write_text("ExecStart=/usr/bin/python3 -I gateway.py --domain awb.example.test\n")
    monkeypatch.setattr(publish, "GATEWAY_UNIT", unit)
    assert publish.main(["host", "awb.example.test"], host) == 1
    assert "of its own" in capsys.readouterr().err
    real = publish.Host(geteuid=lambda: 1000, environ={})
    assert publish.main(["host", "owner.example.test"], real) == 1
    assert "sudo awb web host" in capsys.readouterr().err
