"""Resources on the test tenants over time (awb/tcp/tenants.py): snapshots, events and the questions. The cloud is a
stand-in; every id is invented."""
from __future__ import annotations

import datetime
import json
import os

import pytest

from awb import jobs
from awb.tcp import tenants


def server(rid, created, status="ACTIVE", project="tcp-q7m4", expiry="2026-12-31", flavor="s3.large.2"):
    tags = ["awb-project=%s" % project] if project else []
    if expiry:
        tags.append("awb-expiry=%s" % expiry)
    return {"id": rid, "created": created, "status": status, "flavor": {"id": flavor}, "tags": tags}


class Cloud:
    """A stand-in tenant: what each list call answers, per kind."""

    def __init__(self):
        self.servers = [server("id-a", "2026-08-01T10:00:00Z"), server("id-b", "2026-09-20T09:00:00Z", project="")]
        self.volumes = [{"id": "vol-1", "created_at": "2026-08-01T10:05:00Z", "status": "in-use", "size": 40,
                         "volume_type": "SSD", "metadata": {}}]
        self.eip = jobs.Listing(jobs.LIST, ({"id": "ip-1", "create_time": "2026-08-02 08:00:00", "status": "ACTIVE",
                                              "bandwidth_size": 5},))
        self.broken_volumes = False

    def lister(self, t, region):
        def call(service, path, key, paging):
            if key == "servers":
                return jobs.Listing(jobs.LIST, tuple(self.servers)) if self.servers else jobs.Listing(jobs.EMPTY)
            if key == "volumes":
                if self.broken_volumes:
                    return jobs.Listing(jobs.UNKNOWN, reason="HTTP 503")
                return jobs.Listing(jobs.LIST, tuple(self.volumes)) if self.volumes else jobs.Listing(jobs.EMPTY)
            return self.eip
        return call


def at(day: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(day + "T06:00:00+00:00")


@pytest.fixture
def tenant(home):
    return tenants.add(home, "test-1", "file:/nowhere/keys", ["eu-de"], today="2026-09-01")


def test_add_checks_the_alias_and_the_key_reference(home, tenant):
    assert [t.alias for t in tenants.load(home)] == ["test-1"]
    for alias, keys in (("Test 1", "file:/x"), ("test-2", "plain-key"), ("test-1", "file:/x")):
        with pytest.raises(tenants.TenantError):
            tenants.add(home, alias, keys, ["eu-de"])


def test_the_first_snapshot_sees_everything_with_its_own_creation_time(home, tenant):
    cloud = Cloud()
    snap, events = tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    assert len(snap["items"]) == 4 and {e["event"] for e in events} == {"seen"}
    assert snap["items"][0]["created"].startswith("2026-08-01")
    stored = (tenants.folder(home, "test-1") / "snapshots" / "2026-09-01.json").read_text(encoding="utf-8")
    assert "id-a" not in stored and "vol-1" not in stored, "ids stay in the handle file"
    assert oct(os.stat(tenants.folder(home, "test-1") / "handles.json").st_mode)[-3:] == "600"


def test_a_foreign_tag_value_is_kept_as_a_dash_and_counted(home, tenant):
    """TM0 item 9: a planted awb-project or awb-expiry value not in the Workbench form never reaches the snapshot."""
    from tests import fixtures

    cloud = Cloud()
    cloud.servers.append(server("id-c", "2026-08-03T10:00:00Z", project="call %s" % fixtures.PERSON_FORMS[0],
                                expiry="ask " + fixtures.CUSTOMER_FORMS[0]))
    snap, _ = tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    stored = (tenants.folder(home, "test-1") / "snapshots" / "2026-09-01.json").read_text(encoding="utf-8")
    fixtures.assert_no_fixture_name(stored, "the snapshot")
    planted = [i for i in snap["items"] if i["project"] == "-"]
    assert len(planted) == 1 and planted[0]["expiry"] == "-" and snap["foreign_tags"] == 2
    assert "no project" in tenants.flags(planted[0], datetime.date(2026, 9, 1))
    assert [i["project"] for i in snap["items"]].count("tcp-q7m4") == 1
    lines = tenants.now_lines(home, "test-1", datetime.date(2026, 9, 1))
    assert any("2 tag value(s) not in the Workbench form" in x for x in lines)
    fixtures.assert_no_fixture_name("\n".join(lines), "awb tenant now")


def test_the_next_snapshots_record_what_appeared_changed_and_went(home, tenant):
    cloud = Cloud()
    tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    cloud.servers[0]["status"] = "SHUTOFF"
    cloud.servers.append(server("id-c", "2026-09-02T12:00:00Z", project="tcp-ab12"))
    del cloud.servers[1]
    _, events = tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-02"))
    got = sorted((e["event"], e["handle"]) for e in events)
    assert [g[0] for g in got] == ["appeared", "changed", "gone"]
    changed = [e for e in events if e["event"] == "changed"][0]
    assert changed["fields"] == ["state"] and changed["was"] == {"state": "ACTIVE"}
    lines = tenants.history_lines(home, "test-1", project="tcp-ab12")
    assert len(lines) == 2 and "appeared" in lines[1]


def test_an_unreadable_listing_is_never_read_as_gone(home, tenant):
    cloud = Cloud()
    tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    cloud.broken_volumes = True
    snap, events = tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-02"))
    assert events == [] and snap["unknown"] == {"eu-de": ["evs"]}
    assert "not readable in eu-de: evs" in "\n".join(tenants.now_lines(home, "test-1", datetime.date(2026, 9, 2)))


def test_now_at_list_and_project_answer_from_the_snapshots(home, tenant):
    cloud = Cloud()
    tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    cloud.servers.append(server("id-c", "2026-09-10T12:00:00Z", project="tcp-ab12", expiry="2026-09-15"))
    tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-10"))
    today = datetime.date(2026, 9, 20)
    now = "\n".join(tenants.now_lines(home, "test-1", today))
    assert "5 resources in the snapshot of 2026-09-10" in now and "no project" in now and "expired" in now
    assert "id-c" not in now
    assert tenants.latest(home, "test-1", on="2026-09-05")["date"] == "2026-09-01"
    assert tenants.latest(home, "test-1", on="2026-08-01") is None
    listed = "\n".join(tenants.list_lines(home, today))
    assert "test-1" in listed and "2026-09-10" in listed
    proj = "\n".join(tenants.project_lines(home, "tcp-ab12", today))
    assert "1 running now" in proj and "appeared" in proj
    assert tenants.project_lines(home, "tcp-zz99", today) == ["tcp-zz99: nothing on any tenant"]


def test_a_second_snapshot_on_the_same_day_replaces_it_and_adds_only_new_events(home, tenant):
    cloud = Cloud()
    tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    _, events = tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    assert events == []
    assert len(tenants.snapshots(home, "test-1")) == 1
    assert len(tenants.events(home, "test-1")) == 4


def test_a_key_file_must_be_closed_and_complete(home, tmp_path):
    f = tmp_path / "keys"
    f.write_text("ak=AKINVENTED\n" + "s" + "k=" + "invented" + "-value\n", encoding="utf-8")
    os.chmod(f, 0o644)
    t = tenants.Tenant("test-9", ("eu-de",), "file:%s" % f, "2026-09-01")
    with pytest.raises(tenants.TenantError, match="open to others"):
        tenants.keys_of(t)
    os.chmod(f, 0o600)
    assert tenants.keys_of(t).ak == "AKINVENTED"
    f.write_text("ak=only\n", encoding="utf-8")
    with pytest.raises(tenants.TenantError, match="needs an ak= and an sk="):
        tenants.keys_of(t)


def test_the_command_line_refuses_a_bad_date_and_an_unknown_tenant(home, tenant, capsys):
    assert tenants.main(["now", "test-7"]) == 1 and "no tenant with the alias" in capsys.readouterr().err
    assert tenants.main(["at", "test-1", "yesterday"]) == 1
    assert tenants.main(["list"]) == 0 and "test-1" in capsys.readouterr().out


# --------------------------------------------------------------------------- awb tenant setup (T10)

from tests.tcp_fake import FakeIAM  # noqa: E402

ALIAS = "test-4711"


class FakePass:
    """A stand-in for the pass command: `entries` by name, every call kept with its arguments and its input."""

    def __init__(self, root, entries):
        self.root, self.entries, self.calls = root, dict(entries), []
        for name in self.entries:
            self._file(name)

    def _file(self, name):
        f = self.root / (name + ".gpg")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("encrypted", encoding="utf-8")

    def __call__(self, args, **kw):
        import subprocess

        self.calls.append((list(args), kw.get("input")))
        if args[:2] == ["pass", "show"]:
            value = self.entries.get(args[2])
            return subprocess.CompletedProcess(args, 0 if value else 1, (value or "") + "\n", "")
        if args[:3] == ["pass", "insert", "-m"]:
            self.entries[args[3]] = kw["input"].rstrip("\n")
            self._file(args[3])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError("unexpected command")


@pytest.fixture
def iam_world(home, tmp_path):
    fake = FakeIAM()
    with fake:
        domain, user, password = fake.login
        runner = FakePass(tmp_path / "store", {"awb/admin/%s/domain" % ALIAS: domain,
                                               "awb/admin/%s/user" % ALIAS: user,
                                               "awb/admin/%s/password" % ALIAS: password})
        state = {"loaded": {}, "unlocks": 0}

        def unlock():
            state["unlocks"] += 1
            roles = ["read"] + (["lab"] if (tmp_path / "store" / "awb/tenant" / ALIAS / "lab/ak.gpg").exists() else [])
            state["loaded"] = {ALIAS: {"roles": roles, "secrets": []}}
            return state["loaded"]

        def run(**kw):
            return tenants.setup(home, ALIAS, FakeIAM.DOMAIN_ID, store=tenants.Store(tmp_path / "store", runner),
                                 iam=tenants.Iam("eu-de", endpoint=fake.base), loaded=lambda: state["loaded"],
                                 unlock=unlock, make_lister=Cloud().lister, **kw)

        yield fake, runner, state, run


def test_setup_of_a_fresh_tenant_makes_group_user_key_and_registers_it(home, iam_world):
    fake, runner, state, run = iam_world
    lines, changed = run()
    assert changed and state["unlocks"] == 1
    assert [g["name"] for g in fake.groups.values()] == ["awb-read"]
    reader = [u for u in fake.users.values() if u["name"] == "awb-read-4711"][0]
    assert reader["access_mode"] == "programmatic"
    assert fake.inherited == {(list(fake.groups)[0], "r-guest")}
    assert fake.members == {(list(fake.groups)[0], reader["id"])}
    assert [c["user_id"] for c in fake.credentials.values()] == [reader["id"]]
    inserts = [(a, i) for a, i in runner.calls if a[:2] == ["pass", "insert"]]
    assert [a for a, _ in inserts] == [["pass", "insert", "-m", "awb/tenant/%s/ak" % ALIAS],
                                       ["pass", "insert", "-m", "awb/tenant/%s/sk" % ALIAS]]
    cred = list(fake.credentials.values())[0]
    assert [i for _, i in inserts] == [cred["access"] + "\n", cred["secret"] + "\n"], "values go through stdin"
    t = tenants.get(home, ALIAS)
    assert t.keys == "pass:awb/tenant/%s" % ALIAS and t.regions == ("eu-de",)
    assert tenants.snapshots(home, ALIAS)
    text = "\n".join(lines)
    assert "group awb-read: created" in text and "4 resources" in text
    assert cred["access"] not in text and cred["secret"] not in text
    assert not fake.notifications and "lab key" not in text


def test_a_second_setup_creates_nothing_and_says_so(home, iam_world):
    fake, runner, state, run = iam_world
    run()
    before = len(fake.creates())
    lines, changed = run()
    assert not changed and len(fake.creates()) == before and state["unlocks"] == 1
    assert lines[-1] == "nothing to do: %s was set up already" % ALIAS
    assert len(tenants.load(home)) == 1 and len(fake.credentials) == 1


def test_the_admin_password_never_reaches_an_argument_a_log_or_the_output(home, iam_world, capsys, caplog):
    fake, runner, state, run = iam_world
    password = fake.login[2]
    with caplog.at_level("DEBUG"):
        lines, _ = run(lab_key=True)
        tenants.main(["list"])
    for args, given in runner.calls:
        assert password not in " ".join(args) and password not in (given or "")
    out = capsys.readouterr()
    assert password not in out.out + out.err + "\n".join(lines) + caplog.text
    assert FakeIAM.TOKEN not in out.out + "\n".join(lines)
    for f in tenants.folder(home, ALIAS).rglob("*"):
        if f.is_file():
            assert password not in f.read_text(encoding="utf-8")
    sent = [b for m, p, _, b in fake.calls if password in b]
    assert [p for m, p, _, b in fake.calls if password in b] == ["/v3/auth/tokens"] and len(sent) == 1
    fake.login = (fake.login[0], fake.login[1], "another")
    with pytest.raises(tenants.TenantError) as err:
        run()
    assert password not in str(err.value) and "HTTP 401" in str(err.value)


def test_the_lab_key_is_made_only_when_asked(home, iam_world):
    fake, runner, state, run = iam_world
    run()
    assert [c["user_id"] for c in fake.credentials.values()] != [fake.admin_id]
    assert not any(c["user_id"] == fake.admin_id for c in fake.credentials.values())
    assert "awb/tenant/%s/lab/ak" % ALIAS not in runner.entries
    lines, changed = run(lab_key=True)
    assert changed and sum(c["user_id"] == fake.admin_id for c in fake.credentials.values()) == 1
    assert "awb/tenant/%s/lab/sk" % ALIAS in runner.entries
    assert state["loaded"][ALIAS]["roles"] == ["read", "lab"]
    assert "lab key: created, written to awb/tenant/%s/lab" % ALIAS in lines
    _, changed = run(lab_key=True)
    assert not changed


def test_the_cts_alert_is_created_once_with_the_topic(home, iam_world):
    fake, runner, state, run = iam_world
    topic = "urn:smn:eu-de:%s:awb-alerts" % FakeIAM.PROJECT_ID
    lines, _ = run(alerts=True, topic=topic)
    assert [(n["notification_name"], n["topic_id"]) for n in fake.notifications] == [("awb_new_access_key", topic)]
    assert fake.notifications[0]["operations"][0]["trace_names"] == ["createCredential"]
    _, changed = run(alerts=True, topic=topic)
    assert not changed and len(fake.notifications) == 1
    with pytest.raises(tenants.TenantError, match="another project"):
        fake.notifications.clear()
        run(alerts=True, topic="urn:smn:eu-de:%s:awb-alerts" % ("f" * 32))


def test_setup_refuses_a_missing_admin_login_and_a_bad_domain_id(home, tmp_path):
    store = tenants.Store(tmp_path / "empty", FakePass(tmp_path / "empty", {}))
    with pytest.raises(tenants.TenantError, match="awb/admin/test-1/domain is missing"):
        tenants.setup(home, "test-1", "d" * 32, store=store, iam=tenants.Iam(endpoint="http://127.0.0.1:9"))
    with pytest.raises(tenants.TenantError, match="32 hex"):
        tenants.setup(home, "test-1", "not-an-id", store=store)


# --------------------------------------------------------------------------- --lab-user (T12 part 3, P1)


def test_the_lab_user_gets_the_terraform_services_in_the_lab_project_and_no_iam(home, iam_world):
    fake, runner, state, run = iam_world
    lines, changed = run(lab_user=True)
    assert changed
    group = [g for g in fake.groups.values() if g["name"] == "awb-lab"][0]
    by_name = {r["display_name"]: r["id"] for r in fake.roles}
    project_wide = {by_name[n] for s, n in tenants.LAB_POLICIES if s != "obs"}
    assert fake.project_grants == {(FakeIAM.PROJECT_ID, group["id"], rid) for rid in project_wide}
    assert fake.domain_grants == {(group["id"], by_name["OBS OperateAccess"])}
    assert by_name["IAM FullAccess"] not in {g[2] for g in fake.project_grants} | {g[1] for g in fake.domain_grants}
    assert not any(g[0] == group["id"] for g in fake.inherited)
    lab = [u for u in fake.users.values() if u["name"] == "awb-lab-4711"][0]
    assert lab["access_mode"] == "programmatic" and (group["id"], lab["id"]) in fake.members
    stored = runner.entries["awb/tenant/%s/lab/ak" % ALIAS]
    assert fake.credentials[stored]["user_id"] == lab["id"]
    assert state["loaded"][ALIAS]["roles"] == ["read", "lab"]
    text = "\n".join(lines)
    assert "group awb-lab: created" in text and "OBS OperateAccess on the account: granted" in text
    assert "ECS FullAccess on eu-de: granted" in text and stored not in text
    before = len(fake.creates())
    lines, changed = run(lab_user=True)
    assert not changed and len(fake.creates()) == before and "lab key: present, of awb-lab-4711" in "\n".join(lines)


def test_the_lab_user_replaces_a_lab_key_of_the_admin(home, iam_world):
    fake, runner, state, run = iam_world
    run(lab_key=True)
    admin_key = runner.entries["awb/tenant/%s/lab/ak" % ALIAS]
    assert fake.credentials[admin_key]["user_id"] == fake.admin_id
    lines, changed = run(lab_user=True)
    new_key = runner.entries["awb/tenant/%s/lab/ak" % ALIAS]
    lab = [u for u in fake.users.values() if u["name"] == "awb-lab-4711"][0]
    assert changed and new_key != admin_key and fake.credentials[new_key]["user_id"] == lab["id"]
    replaced = [a for a, _ in runner.calls if a[:2] == ["pass", "insert"] and a[-1] == "-f"]
    assert [a[3] for a in replaced] == ["awb/tenant/%s/lab/ak" % ALIAS, "awb/tenant/%s/lab/sk" % ALIAS]
    assert "stays in IAM until you delete it" in "\n".join(lines) and admin_key not in "\n".join(lines)


def test_the_lab_user_is_refused_with_the_lab_key_and_without_its_policies(home, iam_world):
    fake, runner, state, run = iam_world
    with pytest.raises(tenants.TenantError, match="choose one"):
        run(lab_key=True, lab_user=True)
    fake.roles = [r for r in fake.roles if r["display_name"] != "NAT FullAccess"]
    with pytest.raises(tenants.TenantError, match="no system policy NAT FullAccess"):
        run(lab_user=True)
    assert not fake.project_grants and not fake.domain_grants
    assert not any(u["name"] == "awb-lab-4711" for u in fake.users.values())
