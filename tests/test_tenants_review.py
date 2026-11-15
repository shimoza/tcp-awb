"""Review of awb/tcp/tenants.py: hostile input (trailing newlines, control and line characters), links where a file
should be, a FIFO for a key file, two runs at once and values that must never reach a message. No cloud, no pass, no
network: every stand-in is in memory and every value is invented."""
from __future__ import annotations

import os
import threading
import time

import pytest

from awb.tcp import tenants

SECRET = "sk" + "-invented-" + "value42"
PROJECT_ID = "a" * 32


def snap_of(alias="test-1", day="2026-09-01", n=3):
    items = [{"handle": "ecs-%d" % i, "kind": "ecs", "region": "eu-de", "created": "2026-08-0%dT10:00:00Z" % (i + 1),
              "state": "ACTIVE", "size": "s3.large.2", "project": "tcp-q7m4", "expiry": "", "os": "linux"}
             for i in range(n)]
    return {"alias": alias, "date": day, "taken": day + "T06:00:00Z", "regions": ["eu-de"], "unknown": {},
            "items": items}


@pytest.fixture
def tenant(home):
    return tenants.add(home, "test-1", "file:/nowhere/keys", ["eu-de"], today="2026-09-01")


# --------------------------------------------------------------------------- input


def test_an_alias_with_a_trailing_newline_is_refused(home):
    with pytest.raises(tenants.TenantError):
        tenants.add(home, "test-1\n", "file:/nowhere/keys", ["eu-de"])
    assert tenants.load(home) == []


def test_a_key_reference_with_a_line_character_cannot_forge_a_row(home):
    for sep in ("\n", " ", "\x85"):
        forged = "file:/x" + sep + "test-9\teu-de\tpass:y\t2026-01-01"
        with pytest.raises(tenants.TenantError):
            tenants.add(home, "test-2", forged, ["eu-de"])
    assert tenants.load(home) == []


def test_a_region_with_a_trailing_newline_is_refused(home):
    with pytest.raises(tenants.TenantError):
        tenants.add(home, "test-3", "file:/nowhere/keys", ["eu-de\n"])
    assert tenants.load(home) == []


class _NoLoginIam(tenants.Iam):
    def __init__(self):
        super().__init__("eu-de", endpoint="http://iam.invalid")

    def _send(self, *a, **kw):
        raise tenants.TenantError("the cloud was reached")


class _Store:
    def __init__(self, entries):
        self.entries = dict(entries)

    def has(self, entry):
        return entry in self.entries

    def show(self, entry):
        return self.entries[entry]

    def insert(self, entry, value, replace=False):
        self.entries[entry] = value


def test_a_domain_id_with_a_trailing_newline_is_refused_before_any_login(home):
    store = _Store({"awb/admin/test-1/%s" % k: "invented" for k in ("domain", "user", "password")})
    with pytest.raises(tenants.TenantError) as err:
        tenants.setup(home, "test-1", "d" * 32 + "\n", store=store, iam=_NoLoginIam())
    assert "--domain-id" in str(err.value)


def test_an_alert_topic_with_a_trailing_newline_is_refused(home):
    calls = []

    class Fake(tenants.Iam):
        def call(self, method, service, path, body=None, query=None, token=None, ok=(200, 201, 204), shown=None):
            calls.append(method)
            return 200, {"notifications": []}

    topic = "urn:smn:eu-de:%s:awb-alerts\n" % PROJECT_ID
    with pytest.raises(tenants.TenantError):
        tenants._alert(Fake(), {"project": {"id": PROJECT_ID}, "_id": "tok"}, "eu-de", topic, [])
    assert "POST" not in calls


# --------------------------------------------------------------------------- values never in a message


def test_an_unknown_alias_is_not_echoed(home):
    hostile = "Test\x1b[2J" + SECRET
    with pytest.raises(tenants.TenantError) as err:
        tenants.get(home, hostile)
    assert SECRET not in str(err.value) and "\x1b" not in str(err.value)


def test_the_error_code_of_an_answer_is_not_echoed():
    class Fake(tenants.Iam):
        def _send(self, *a, **kw):
            return 400, {"error": {"code": "IAM.01\nsk=" + SECRET}}, {}

    with pytest.raises(tenants.TenantError) as err:
        Fake("eu-de", endpoint="http://iam.invalid").call("GET", "iam", "/v3/users")
    assert SECRET not in str(err.value) and "\n" not in str(err.value)


def test_a_key_in_a_url_path_never_reaches_an_error():
    path = "/v3.0/OS-CREDENTIAL/credentials/AK " + SECRET + "\x01"
    with pytest.raises(tenants.TenantError) as err:
        tenants.Iam(endpoint="http://127.0.0.1:9", timeout=2).call("GET", "iam", path)
    assert SECRET not in str(err.value)


def test_the_key_lookup_quotes_the_stored_key():
    sent = []

    class Fake(tenants.Iam):
        def _send(self, method, service, path, body=None, query=None, token=None):
            sent.append(path)
            return 200, {"credential": {"user_id": "u-old"}}, {}

    access = "AK " + SECRET
    store = _Store({"awb/tenant/test-1/ak": access, "awb/tenant/test-1/sk": "s" + "k"})
    owner = tenants._key_owner(Fake("eu-de", endpoint="http://iam.invalid"), store, "awb/tenant/test-1", None, "u-a")
    assert owner == "u-old"
    assert sent and " " not in sent[-1] and access not in sent[-1]


def test_a_domain_name_from_the_token_is_printed_without_control_characters(home):
    from tests.test_tenant_setup_adopt import ALIAS, FakeIam, FakeStore, lister

    fake = FakeIam()
    fake.token_domain = dict(fake.token_domain, name="fake\x1b[31mdomain\nforged line")
    domain, user, password = fake.login_of
    store = FakeStore({"awb/admin/%s/domain" % ALIAS: domain, "awb/admin/%s/user" % ALIAS: user,
                       "awb/admin/%s/password" % ALIAS: password})
    lines, _ = tenants.setup(home, ALIAS, None, store=store, iam=fake, loaded=lambda: {}, unlock=lambda: {},
                             make_lister=lister)
    text = "\n".join(lines)
    assert "\x1b" not in text and "forged line" not in lines


# --------------------------------------------------------------------------- links, FIFOs and broken files


def test_a_link_in_place_of_the_register_is_not_written_through(home, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("kept\n", encoding="utf-8")
    tenants.root(home).mkdir(parents=True, exist_ok=True)
    os.symlink(outside, tenants.root(home) / tenants.REGISTER)
    with pytest.raises((tenants.TenantError, OSError)):
        tenants.add(home, "test-1", "file:/nowhere/keys", ["eu-de"])
    assert outside.read_text(encoding="utf-8") == "kept\n"


def test_a_link_planted_at_the_temp_name_of_a_snapshot_is_not_written_through(home, tenant, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("kept\n", encoding="utf-8")
    snaps = tenants.folder(home, "test-1") / tenants.SNAPSHOTS
    snaps.mkdir(parents=True)
    os.symlink(outside, snaps / ".2026-09-01.json.tmp")
    tenants.record(home, snap_of())
    assert outside.read_text(encoding="utf-8") == "kept\n"
    assert tenants.latest(home, "test-1")["date"] == "2026-09-01"


def test_a_link_in_place_of_the_events_file_is_not_appended_to(home, tenant, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("kept\n", encoding="utf-8")
    tenants.folder(home, "test-1").mkdir(parents=True)
    os.symlink(outside, tenants.folder(home, "test-1") / tenants.EVENTS)
    with pytest.raises(OSError):
        tenants.record(home, snap_of())
    assert outside.read_text(encoding="utf-8") == "kept\n"


def test_a_fifo_as_the_key_file_is_refused_without_hanging(home, tmp_path):
    fifo = tmp_path / "keys.fifo"
    os.mkfifo(fifo, 0o600)
    t = tenants.Tenant("test-1", ("eu-de",), "file:%s" % fifo, "2026-09-01")
    result = {}

    def read():
        try:
            tenants.keys_of(t)
        except tenants.TenantError as err:
            result["error"] = str(err)

    th = threading.Thread(target=read, daemon=True)
    th.start()
    th.join(5)
    hung = th.is_alive()
    if hung:                     # release the reader of the old code
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        th.join(5)
    assert not hung and "cannot be read" in result.get("error", "")


def test_a_register_that_is_not_utf8_is_a_tenant_error(home):
    tenants.root(home).mkdir(parents=True, exist_ok=True)
    (tenants.root(home) / tenants.REGISTER).write_bytes(b"alias\tregions\tkeys\tadded\n\xff\xfe\n")
    with pytest.raises(tenants.TenantError):
        tenants.load(home)


def test_a_broken_snapshot_ends_with_exit_1_and_none_of_its_text(home, tenant, capsys):
    snaps = tenants.folder(home, "test-1") / tenants.SNAPSHOTS
    snaps.mkdir(parents=True)
    (snaps / "2026-09-01.json").write_text("{not json " + SECRET, encoding="utf-8")
    assert tenants.main(["now", "test-1"]) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.err + captured.out


def test_history_refuses_a_project_that_is_no_code(home, tenant, capsys):
    assert tenants.main(["history", "test-1", "--project", "x\x1b[2J" + SECRET]) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.err + captured.out


# --------------------------------------------------------------------------- two runs at once


def test_two_adds_at_once_keep_one_row_and_one_head_line(home, monkeypatch):
    real = tenants.load

    def slow(p):
        out = real(p)
        time.sleep(0.4)
        return out

    monkeypatch.setattr(tenants, "load", slow)
    errors = []

    def run():
        try:
            tenants.add(home, "test-1", "file:/nowhere/keys", ["eu-de"], today="2026-09-01")
        except tenants.TenantError as err:
            errors.append(str(err))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    lines = (tenants.root(home) / tenants.REGISTER).read_text(encoding="utf-8").splitlines()
    assert lines.count("\t".join(tenants.FIELDS)) == 1
    assert sum(1 for line in lines if line.startswith("test-1\t")) == 1
    assert len(errors) == 1 and "taken" in errors[0]


def test_two_snapshots_at_once_record_each_event_once(home, tenant, monkeypatch):
    real = tenants.diff

    def slow(before, after):
        out = real(before, after)
        time.sleep(0.4)
        return out

    monkeypatch.setattr(tenants, "diff", slow)
    errors = []

    def run():
        try:
            tenants.record(home, snap_of())
        except Exception as err:          # the old fixed temp name lets one run lose its file
            errors.append(type(err).__name__)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    events = tenants.events(home, "test-1")
    assert not errors
    assert len(events) == 3 and {e["event"] for e in events} == {"seen"}
    left = [f.name for f in (tenants.folder(home, "test-1") / tenants.SNAPSHOTS).iterdir() if f.name.endswith(".tmp")]
    assert left == []

