"""The review of 2026-10-10, group g2 (the owner level: 5f25ac2): each test is the review's test of
`presentations/review-2026-10-10/tests/test_r10_g2_owner.py` with the prefix `test_g2_owner_N_` dropped and the same
asserts. Invented logins and domains only."""
import http.client
import json
import threading

from tests.test_web_owner_api import OWNER, ROUNDS, site  # noqa: F401  (the fixture of the owner level tests)
from awb.tcp.web import gateway, tenant_api, users


def test_one_wrong_password_a_second_keeps_the_owner_out_for_ten_minutes(tmp_path):
    """Promise (5f25ac2, design section 3): each failure is a delay of 1 to 60 seconds and never a lock; no number
    of wrong tries locks the owner out. Attack: anyone on the internet who knows the owner's login sends one wrong
    password per second from one address. A try inside the delay is not counted and costs nothing, the first try
    after it starts the next 60 seconds, the address never reaches 8 failures a minute. The owner, with the right
    password and a current code, never gets in."""
    users_file = tmp_path / "users.json"
    password = users.change(users_file, "add", "owner1", False, rounds=ROUNDS, level="owner")
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


class Inventory:
    def get(self, refresh=False):
        return {"tenants": [{"alias": "test-1", "domain_name": "sample-domain-00000001", "status": "complete"}]}


def test_the_owner_tenant_route_is_not_open_on_the_loopback_port():
    """Promise (5f25ac2, tenant_api docstring and design section 4): GET /api/owner/tenants with the IAM domain
    names is reached only through the gateway's owner host; owner routes never on a TCP port; the domain name stays
    on the owner level (RT-17). Attack: a hostile project session (the work user, loopback TCP to every port) asks
    127.0.0.1:8183 directly, no cookie, no level header."""
    server = tenant_api.make_server(0, Inventory())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        c.request("GET", "/api/owner/tenants")
        r = c.getresponse()
        status, body = r.status, r.read()
        c.close()
    finally:
        server.shutdown()
        server.server_close()
    assert status != 200 and b"sample-domain-00000001" not in body, (status, body)


def test_a_same_site_request_with_the_owner_cookie_reaches_no_backend(site):  # noqa: F811
    """Promise (design section 1, RT-01): on its own host the owner cookie is never sent with the main page's
    requests, so a script of the queue-built main page cannot act with it. Attack: the main host and the owner host
    are one site (the owner host is one level under the zone), so SameSite=Strict still sends __Host-awb-owner on a
    no-cors request the main page makes. The gateway checks Sec-Fetch-Site on owner POSTs only; a GET with a side
    effect (the tenant refresh, a call into the cloud) and the probe whether the owner is signed in go through."""
    token = site.owner_session()
    before = len(site.tcp.calls)
    status, _, _ = site.request("/api/owner/tenants?refresh=1", host=OWNER, headers={
        "Cookie": gateway.OWNER_COOKIE + "=" + token, "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Dest": "image"})
    reached = site.tcp.calls[before:]
    assert status == 403 and reached == [], (status, [c[1] for c in reached])
