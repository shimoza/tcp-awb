"""The review of 2026-10-10, group g2 (the owner level: 5f25ac2): each test is the review's test of
`presentations/review-2026-10-10/tests/test_r10_g2_owner.py` with the prefix `test_g2_owner_N_` dropped and the same
asserts. Invented logins and domains only."""
import json

from awb.tcp.web import gateway, users


def test_one_wrong_password_a_second_keeps_the_owner_out_for_ten_minutes(tmp_path):
    """Promise (5f25ac2, design section 3): each failure is a delay of 1 to 60 seconds and never a lock; no number
    of wrong tries locks the owner out. Attack: anyone on the internet who knows the owner's login sends one wrong
    password per second from one address. A try inside the delay is not counted and costs nothing, the first try
    after it starts the next 60 seconds, the address never reaches 8 failures a minute. The owner, with the right
    password and a current code, never gets in."""
    users_file = tmp_path / "users.json"
    password = users.change(users_file, "add", "owner1", False, rounds=1000, level="owner")
    secret = json.loads(users_file.read_text())["users"]["owner1"]["totp"]
    clock = [1_800_000_000.0]
    auth = gateway.Auth(tmp_path / "auth.json", users_file, None, clock=lambda: clock[0])
    start = clock[0]
    owner_results = []
    for half in range(0, 2 * 600):              # ten minutes, the attacker every half second
        clock[0] = start + half * 0.5
        auth.sign_in_owner("owner1", "wrong-password-123", "000000", "203.0.113.5")
        if half % 14 == 0:                     # the owner every seven seconds, a quarter second later
            clock[0] += 0.25
            owner_results.append(auth.sign_in_owner("owner1", password,
                                                    gateway.totp_code(secret, int(clock[0] // 30)), "198.51.100.7"))
    assert "ok" in owner_results, "the owner stayed out for ten minutes: %d tries, all %s" % (
        len(owner_results), sorted(set(owner_results)))
