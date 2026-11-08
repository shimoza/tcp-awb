"""awb tenant setup adopts the user of an existing read key (B-050): the domain from the admin token, the owner of
the read key in the store put into awb-read in place of a new user and key, a snapshot after it. IAM and the
password store are stand-ins in memory (no loopback); every id and key is invented."""
from __future__ import annotations

import pytest

from awb import jobs
from awb.tcp import tenants

ALIAS = "test-10491"
DOMAIN_ID = "3f9a1c" + "d" * 26
TOKEN = "fake-token-of-the-admin"
OLD_KEY, OLD_SECRET = "AKFAKEOLD01", "SK-invented-old-read"


class FakeStore:
    """The password store by entry name; `inserts` keeps the names written."""

    def __init__(self, entries):
        self.entries, self.inserts = dict(entries), []

    def has(self, entry):
        return entry in self.entries

    def show(self, entry):
        if entry not in self.entries:
            raise tenants.TenantError("the password store gives nothing for %s" % entry)
        return self.entries[entry]

    def insert(self, entry, value, replace=False):
        self.inserts.append(entry)
        self.entries[entry] = value


class FakeIam(tenants.Iam):
    """IAM of one account in memory, behind the real login and call of tenants.Iam: the password token (domain
    scope by id or by name), roles, groups, the inherited grant, users, members and access keys. Every request is
    kept in `calls` as (method, path, query)."""

    def __init__(self):
        super().__init__("eu-de", endpoint="http://iam.invalid")
        self.login_of = ("fake-domain", "fake-admin", "pw-invented-7")
        self.token_domain = {"id": DOMAIN_ID, "name": "fake-domain"}
        self.users = {"u-admin": {"id": "u-admin", "name": "fake-admin"},
                      "u-old": {"id": "u-old", "name": "fake-reader"}}
        self.groups, self.inherited, self.members = {}, set(), set()
        self.credentials = {OLD_KEY: {"access": OLD_KEY, "user_id": "u-old", "secret": OLD_SECRET}}
        self.calls, self.fail_lookup, self._n = [], None, 0

    def _id(self, kind):
        self._n += 1
        return "%s-%d" % (kind, self._n)

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m in ("POST", "PUT", "DELETE", "PATCH") and p != "/v3/auth/tokens"]

    def _send(self, method, service, path, body=None, query=None, token=None):
        q = dict(query or {})
        self.calls.append((method, path, q))
        token = self.token if token is None else token
        if path == "/v3/auth/tokens" and method == "POST":
            pw = body["auth"]["identity"]["password"]["user"]
            if (pw["domain"]["name"], pw["name"], pw["password"]) != self.login_of:
                return 401, {"error": {"code": 401}}, {}
            scope = body["auth"]["scope"]["domain"]
            if scope not in ({"id": DOMAIN_ID}, {"name": "fake-domain"}):
                return 401, {"error": {"code": 401}}, {}
            return 201, {"token": {"user": {"id": "u-admin"}, "domain": dict(self.token_domain)}}, \
                {"X-Subject-Token": TOKEN}
        if token != TOKEN:
            return 401, {"error": {"code": 401}}, {}
        parts = path.strip("/").split("/")
        if path == "/v3/auth/domains":
            return 200, {"domains": [{"id": DOMAIN_ID, "name": "fake-domain"}]}, {}
        if path == "/v3/roles":
            roles = [{"id": "r-guest", "name": "readonly"}, {"id": "r-admin", "name": "te_admin"}]
            return 200, {"roles": [r for r in roles if r["name"] == q.get("name", r["name"])]}, {}
        if path == "/v3/groups":
            if method == "POST":
                g = dict(body["group"], id=self._id("g"))
                self.groups[g["id"]] = g
                return 201, {"group": g}, {}
            return 200, {"groups": [g for g in self.groups.values() if g["name"] == q.get("name", g["name"])]}, {}
        if path.startswith("/v3/OS-INHERIT/domains/%s/" % DOMAIN_ID):
            key = (parts[5], parts[7])
            if method == "PUT":
                self.inherited.add(key)
                return 204, {}, {}
            return (204 if key in self.inherited else 404), {}, {}
        if path == "/v3/users":
            if method == "POST":
                u = dict(body["user"], id=self._id("u"))
                self.users[u["id"]] = u
                return 201, {"user": u}, {}
            return 200, {"users": [u for u in self.users.values() if u["name"] == q.get("name", u["name"])]}, {}
        if path.startswith("/v3.0/OS-USER/users/") and method == "PUT":
            self.users[parts[3]].update(body["user"])
            return 200, {"user": self.users[parts[3]]}, {}
        if len(parts) == 5 and parts[:2] == ["v3", "groups"] and parts[3] == "users":
            key = (parts[2], parts[4])
            if method == "PUT":
                self.members.add(key)
                return 204, {}, {}
            return (204 if key in self.members else 404), {}, {}
        if path == "/v3.0/OS-CREDENTIAL/credentials":
            if method == "POST":
                ak = "AKFAKENEW%02d" % self._n
                self._n += 1
                self.credentials[ak] = {"access": ak, "user_id": body["credential"]["user_id"],
                                        "secret": "SK-invented-%s" % ak}
                return 201, {"credential": dict(self.credentials[ak])}, {}
            return 200, {"credentials": [{"access": c["access"], "user_id": c["user_id"]}
                                         for c in self.credentials.values() if c["user_id"] == q.get("user_id")]}, {}
        if len(parts) == 4 and parts[:3] == ["v3.0", "OS-CREDENTIAL", "credentials"] and method == "GET":
            if self.fail_lookup:
                return self.fail_lookup, {"error": {"code": "IAM.0001"}}, {}
            c = self.credentials.get(parts[3])
            if not c:
                return 404, {"error": {"code": "IAM.0002"}}, {}
            return 200, {"credential": {"access": c["access"], "user_id": c["user_id"], "status": "active"}}, {}
        return 404, {"error_msg": "no such path"}, {}


def lister(t, region):
    return lambda service, path, key, paging: jobs.Listing(jobs.EMPTY)


@pytest.fixture
def world(home):
    fake = FakeIam()
    domain, user, password = fake.login_of
    store = FakeStore({"awb/admin/%s/domain" % ALIAS: domain, "awb/admin/%s/user" % ALIAS: user,
                       "awb/admin/%s/password" % ALIAS: password,
                       "awb/tenant/%s/ak" % ALIAS: OLD_KEY, "awb/tenant/%s/sk" % ALIAS: OLD_SECRET})
    state = {"unlocks": 0, "estimates": 0}

    def unlock():
        state["unlocks"] += 1
        return {}

    def estimate():
        state["estimates"] += 1
        return ["cost estimate: invented"]

    def run(domain_id=None, **kw):
        return tenants.setup(home, ALIAS, domain_id, store=store, iam=fake, loaded=lambda: {ALIAS: {"roles": ["read"]}},
                             unlock=unlock, make_lister=lister, estimate=estimate, **kw)

    return fake, store, state, run


def test_without_domain_id_the_domain_comes_from_the_token_and_setup_goes_on(home, world):
    fake, store, state, run = world
    lines, changed = run()
    assert changed and tenants.get(home, ALIAS).keys == "pass:awb/tenant/%s" % ALIAS
    text = "\n".join(lines)
    assert "domain fake-domain (id 3f9a1c...): from the admin token" in lines
    assert DOMAIN_ID not in text
    assert fake.groups and all(g["domain_id"] == DOMAIN_ID for g in fake.groups.values())
    login = [c for c in fake.calls if c[1] == "/v3/auth/tokens"]
    assert len(login) == 1
    fake.token_domain = {}
    lines, _ = run()
    assert "domain fake-domain (id 3f9a1c...): from the admin token" in lines
    assert ("GET", "/v3/auth/domains", {}) in fake.calls


def test_a_read_key_of_a_user_in_no_group_adopts_that_user_without_a_new_user_or_key(home, world):
    fake, store, state, run = world
    lines, changed = run()
    group = [g for g in fake.groups.values() if g["name"] == "awb-read"][0]
    assert changed and fake.members == {(group["id"], "u-old")}
    assert sorted(fake.users) == ["u-admin", "u-old"], "no new user"
    assert list(fake.credentials) == [OLD_KEY] and store.inserts == [], "no new key"
    assert "read key: its user adopted into awb-read" in lines
    assert not any(m == "DELETE" for m, _, _ in fake.calls)
    assert tenants.snapshots(home, ALIAS) and state["estimates"] == 1 and state["unlocks"] == 1
    assert "fake-reader" not in "\n".join(lines)


def test_a_snapshot_runs_after_the_adoption_when_one_was_there_already(home, world):
    fake, store, state, run = world
    tenants.add(home, ALIAS, "pass:awb/tenant/%s" % ALIAS, ["eu-de"])
    tenants.snapshot(home, tenants.get(home, ALIAS), lister)
    lines, changed = run()
    assert changed and "snapshot: 0 resources" in lines and state["estimates"] == 1


def test_the_missing_group_is_created_with_the_inherited_tenant_guest_grant(home, world):
    fake, store, state, run = world
    assert not fake.groups
    lines, _ = run()
    group = [g for g in fake.groups.values() if g["name"] == "awb-read"]
    assert len(group) == 1 and fake.inherited == {(group[0]["id"], "r-guest")}
    assert "group awb-read: created" in lines and "Tenant Guest on all projects: granted" in lines
    assert ("PUT", "/v3/OS-INHERIT/domains/%s/groups/%s/roles/r-guest/inherited_to_projects"
            % (DOMAIN_ID, group[0]["id"]), {}) in fake.calls


def test_a_second_run_has_nothing_to_do(home, world):
    fake, store, state, run = world
    run()
    writes, snaps = len(fake.writes()), len(tenants.snapshots(home, ALIAS))
    lines, changed = run()
    assert not changed and len(fake.writes()) == writes and state["unlocks"] == 1 and state["estimates"] == 1
    assert "read key: its user is in awb-read" in lines
    assert lines[-1] == "nothing to do: %s was set up already" % ALIAS
    assert len(tenants.snapshots(home, ALIAS)) == snaps


def test_the_access_key_never_appears_in_the_output(home, world):
    fake, store, state, run = world
    lines, _ = run()
    lines += run()[0]
    text = "\n".join(lines)
    assert OLD_KEY not in text and OLD_SECRET not in text
    assert ("GET", "/v3.0/OS-CREDENTIAL/credentials/%s" % OLD_KEY, {}) in fake.calls
    fake.members.clear()
    fake.fail_lookup = 500
    with pytest.raises(tenants.TenantError) as err:
        run()
    assert OLD_KEY not in str(err.value) and "HTTP 500" in str(err.value)


def test_a_key_of_the_admin_or_one_iam_does_not_know_is_refused_and_nothing_is_written(home, world):
    fake, store, state, run = world
    fake.credentials[OLD_KEY]["user_id"] = "u-admin"
    with pytest.raises(tenants.TenantError, match="a key of the admin user"):
        run()
    del fake.credentials[OLD_KEY]
    with pytest.raises(tenants.TenantError, match="IAM does not know") as err:
        run()
    assert OLD_KEY not in str(err.value)
    assert fake.writes() == [] and store.inserts == []


def test_a_read_key_of_the_read_user_stays_the_path_of_t10(home, world):
    fake, store, state, run = world
    del store.entries["awb/tenant/%s/ak" % ALIAS], store.entries["awb/tenant/%s/sk" % ALIAS]
    lines, _ = run(domain_id=DOMAIN_ID)
    reader = [u for u in fake.users.values() if u["name"] == "awb-read-10491"][0]
    assert store.entries["awb/tenant/%s/ak" % ALIAS] in {a for a, c in fake.credentials.items()
                                                       if c["user_id"] == reader["id"]}
    assert not any(line.startswith("domain ") for line in lines)
    lines, changed = run(domain_id=DOMAIN_ID)
    assert not changed and "user awb-read-10491: present" in lines
    assert not any(p.startswith("/v3.0/OS-CREDENTIAL/credentials/") for _, p, _ in fake.calls)
