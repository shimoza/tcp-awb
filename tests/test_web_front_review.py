"""The review of 2026-10-10, group g1 (the console's front door: d0f6f14, 2b83362): each test is the review's test of
`presentations/review-2026-10-10/tests/test_r10_g1_front.py` with the prefix `test_g1_front_` dropped and the same
asserts. Invented logins only."""
import hashlib
import http.client
import json
import re
import threading
import time
from urllib.parse import urlencode

import pytest

from awb.tcp.web import gateway
from awb.tcp.web import users as _users

DOMAIN = "awb.example.test"
ROUNDS = 1000
T0 = 1_800_000_000.0


def write_auth(path, username="awb", password="short-test-pw", salt=b"test-only-salt"):
    path.write_text(json.dumps({"username": username, "salt": salt.hex(), "rounds": ROUNDS,
                                "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ROUNDS).hex()}))


@pytest.fixture
def readers(tmp_path):
    write_auth(tmp_path / "auth.json")
    path = tmp_path / "users.json"
    pw = {login: _users.change(path, "add", login, False, rounds=ROUNDS) for login in ("reader1", "reader2")}
    owner = _users.change(path, "add", "owner1", False, rounds=ROUNDS, level="owner")
    return tmp_path, path, pw, owner


def _auth(tmp_path, path, clock, log=False):
    return gateway.Auth(tmp_path / "auth.json", path, tmp_path / "signin.log" if log else None, clock=clock)


# finding 10: the main host tells the owner's login name apart from an unknown one -----------------------------

def test_owner_login_answers_like_an_unknown_login(readers):
    """d0f6f14 promises that an unknown login is answered exactly like a wrong password, and the design's test list
    says a known and an unknown name answer alike. on the main host the owner entry never reaches the cooldown, so
    six wrong tries name the owner login: an unknown name gets 'cooldown' (429), the owner name keeps 'fail' (200)."""
    tmp_path, path, pw, owner = readers
    now = [T0]
    auth = _auth(tmp_path, path, lambda: now[0])
    answers = {"owner1": [], "nobody7": []}
    n = 0
    for _ in range(6):
        for name in answers:
            n += 1
            answers[name].append(auth.sign_in(name, "wrong", "2001:db8:%x::1" % n))
        now[0] += 61        # past the owner's own delay, so the delay is not what differs
    assert answers["owner1"] == answers["nobody7"], answers


# finding 11: parallel guesses from many addresses run past the five-failure series ----------------------------

def test_parallel_guesses_stop_at_the_series(readers, monkeypatch):
    """d0f6f14: per login 5 failures within 15 minutes make it wait. the per-login wait is checked before the pbkdf2
    but the failure is recorded only after it, so guesses sent at once from distinct addresses all pass the check:
    16 parallel guesses for one reader name get 16 password checks instead of at most 5."""
    tmp_path, path, pw, owner = readers
    auth = _auth(tmp_path, path, time.time)
    real = hashlib.pbkdf2_hmac
    checks = []

    def slow(*a, **k):
        checks.append(1)
        time.sleep(0.15)
        return real(*a, **k)

    monkeypatch.setattr(gateway.hashlib, "pbkdf2_hmac", slow)
    barrier = threading.Barrier(16)
    results = []

    def guess(i):
        barrier.wait()
        results.append(auth.sign_in("reader1", "guess-%d" % i, "2001:db8:77:%x::1" % (i + 1)))

    threads = [threading.Thread(target=guess, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(checks) <= gateway.SERIES, (len(checks), sorted(results))


# finding 4: one wrong try a minute from a third party keeps the owner out of the owner host -------------------

def test_a_third_party_cannot_keep_the_owner_out(readers):
    """the design: 'no number of wrong codes locks the owner out' and 'never a hard lock'. a try inside the delay is
    answered 'fail' without checking the password, so an attacker who sends one wrong password for the owner's name
    the moment its delay ends keeps the delay armed for good: ten correct owner sign-ins in ten minutes all fail."""
    tmp_path, path, pw, owner = readers
    secret = json.loads(path.read_text())["users"]["owner1"]["totp"]
    now = [T0]
    auth = _auth(tmp_path, path, lambda: now[0])
    cell = auth.delays.cell("owner:owner1")

    def attacker():
        now[0] = max(now[0], auth.delays.until[cell]) + 0.01
        assert auth.sign_in_owner("owner1", "wrong", "000000", "203.0.113.9") == "fail"

    for _ in range(7):                  # ramp the name's delay to its 60 s ceiling
        attacker()
    results = []
    for _ in range(10):
        attacker()
        now[0] += 30                    # the owner signs in half a minute later, with the right password and code
        code = gateway.totp_code(secret, int(now[0] // gateway.TOTP_STEP))
        results.append(auth.sign_in_owner("owner1", owner, code, "198.51.100.4"))
        if results[-1] == "ok":
            break
    assert "ok" in results, results


# finding 12: the tcp listeners take the per-address key from a header any local process sets -----------------

def test_a_local_client_cannot_pick_its_own_address_key(readers):
    """2b83362 keeps --ports 8080 8081 in the unit for one release, and every local user (the work user too)
    reaches them. client_key() trusts CF-Connecting-IP on every listener, so a local client that sends a new value
    per try never meets the 8-a-minute limit: the ninth failure from one loopback client is still answered 200."""
    tmp_path, path, pw, owner = readers
    auth = _auth(tmp_path, path, time.time)
    (tmp_path / "index.html").write_text("<html>x</html>")
    server = gateway.make_server(0, auth, tmp_path / "index.html", 1, 1, domain=DOMAIN)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def attempt(login, address):
        c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        c.request("GET", "/login", headers={"Host": DOMAIN})
        r = c.getresponse()
        page, cookie = r.read(), r.getheader("Set-Cookie").split(";")[0]
        c.close()
        csrf = re.search(rb'name="csrf" value="([^"]+)"', page).group(1).decode()
        body = urlencode({"csrf": csrf, "username": login, "password": "wrong", "next": "/"})
        c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        c.request("POST", "/login", body=body, headers={
            "Host": DOMAIN, "Cookie": cookie, "Origin": "https://" + DOMAIN, "CF-Connecting-IP": address,
            "Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    try:
        statuses = [attempt("spray%d" % i, "192.0.2.%d" % (i + 1)) for i in range(9)]
    finally:
        server.shutdown()
        server.server_close()
    assert statuses[-1] == 429, statuses
