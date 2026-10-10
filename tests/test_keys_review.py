"""The key service against a hostile project session, from the attacker review of 2026-10-10; each part names its
finding.

Every key, bucket and customer code here is invented; customer codes come from tests/fixtures.py.
"""
from __future__ import annotations

import contextlib
import fnmatch
import os
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from awb import jobs, projects
from awb.tcp import keys, xchg
from tests import fixtures as fx
from tests.obs_fake import FakeOBS

AK, SK = "AKLABFAKE", "sk-lab-fake-secret"
GOAL = "move two app clusters to managed k8s"


@contextlib.contextmanager
def short_dir():
    d = tempfile.mkdtemp(prefix="awbk", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    from awb.tcp import throttle
    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)


@pytest.fixture
def buckets():
    with FakeOBS("awb-lab-eu-de", AK, SK) as lab, FakeOBS("awb", AK, SK) as own:
        yield lab, own


@pytest.fixture
def svc(home, buckets, tmp_path, monkeypatch):
    lab, own = buckets
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log",
                         obs_endpoints={lab.bucket: lab.endpoint, own.bucket: own.endpoint})
        s._notify = lambda subject, message: True
        s._registered = lambda: {"test-1": ("eu-de",)}
        s.start()
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        keys.unlock(s.admin_path, ["test-1/lab/ak", "test-1/lab/sk"], reader={"test-1/lab/ak": AK,
                    "test-1/lab/sk": SK}.get, settings={"bucket_tenant": "test-1", "lab_bucket": lab.bucket,
                                                         "owner_bucket": own.bucket, "max_mb": "1"})
        try:
            yield s
        finally:
            s.stop()


@pytest.fixture
def lab_project(home, register_path, monkeypatch):
    pr = projects.spawn(home, "lab", GOAL, None, register_path)
    monkeypatch.chdir(pr.path)
    return pr


@pytest.fixture
def cust_project(home, register_path, monkeypatch):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    monkeypatch.chdir(pr.path)
    return pr


def _plant_row(home, code, customer):
    """Planted: a session rewrites the customer of its own row in projects.tsv (the work user's file)."""
    projects._save(home, [replace(r, customer=customer) if r.code == code else r for r in projects.load(home)])


class _Minted:
    """IAM's securitytokens call: records the body and answers a temporary key."""

    def __init__(self):
        self.sent = []

    def method(self):
        return lambda client, *a, **k: self(client, *a, **k)

    def __call__(self, client, method, service, path, query=None, body=None, headers=None):
        from awb.tcp.cloud import Response

        self.sent.append(body)
        return Response(201, {"credential": {"access": "TMPAKR10Q", "secret": "tmp-secret-value",
                                             "securitytoken": "tmp-token-value"}}, "")


def _covers(stmt: dict, action: str, resource: str) -> bool:
    actions = stmt.get("Action") or []
    if not any(fnmatch.fnmatchcase(action, a) for a in actions):
        return False
    resources = stmt.get("Resource")
    if resources is None:          # no Resource: every resource the user may reach
        return True
    return any(fnmatch.fnmatchcase(resource, r) for r in resources)


def _policy_reaches(policy: dict, action: str, resource: str) -> bool:
    stmts = policy.get("Statement") or []
    if any(s.get("Effect") == "Deny" and _covers(s, action, resource) for s in stmts):
        return False
    return any(s.get("Effect") == "Allow" and _covers(s, action, resource) for s in stmts)


# --------------------------------------------------------------------------- the lease and the exchange buckets (finding 1)


def test_a_lease_of_the_bucket_tenant_never_reaches_the_owner_bucket(svc, buckets, monkeypatch):
    """Promise (de44798, DESIGN-terraform.md section 1): the leased key is for the services of a project's
    Terraform set, scoped to the lab project; the owner bucket (originals of the owner inbox) is reached only
    through the service's checked paths (take_owner, web_read). The lab key of bucket_tenant reaches both buckets
    by configuration (xchg docstring), and the lease is minted from that same lab key with obs:*:* and no
    Resource. Attack: a granted project session leases the bucket tenant and reads inbox/ of the owner bucket
    with the temporary key directly, past the intake."""
    from awb.tcp import cloud

    from types import SimpleNamespace

    _, own = buckets
    minted = _Minted()
    monkeypatch.setattr(cloud.Client, "request", minted.method())
    row = SimpleNamespace(code="tcp-ab2c", kind="project", customer=projects.NO_CUSTOMER)
    svc._project_row = lambda code: row if code == "tcp-ab2c" else None
    svc._granted = lambda code: code == "tcp-ab2c"       # the owner granted the lab project, as for any lab work
    svc._console_uid = lambda: None
    session = os.getuid() + 1
    try:
        a = svc._lease({"tenant": "test-1", "minutes": 60, "project": "tcp-ab2c"}, session)
    except keys.Refused:
        return                       # a lease of the bucket tenant refused: the promise holds
    assert a["ok"]
    policy = minted.sent[-1]["auth"]["identity"]["policy"]
    for action in ("obs:object:GetObject", "obs:bucket:ListBucket"):
        for resource in ("obs:*:*:object:%s/inbox/task.txt" % own.bucket, "obs:*:*:bucket:%s" % own.bucket):
            assert not _policy_reaches(policy, action, resource), (
                "the leased key of the bucket tenant may %s on %s" % (action, resource))


# --------------------------------------------------------------------------- a project never granted (finding 5)


def test_a_row_never_granted_does_not_choose_the_customer_of_an_owner_take(svc, buckets, home, lab_project):
    """Promise (39b9af4, xchg.project_customer): the work user's row never chooses the customer of the service;
    the residual of F5 (a session that rewrites its row picks the customer of a top-level owner-inbox file) is
    closed. Attack: a project the owner never granted (every query project, or a row the session appends) writes
    a customer into its row and takes the owner's top-level inbox file under that customer."""
    _, own = buckets
    own.objects["inbox/task.txt"] = b"Migrate the database cluster.\n"
    _plant_row(home, lab_project.code, fx.CUSTOMER_CODE)
    a = keys.request(svc.call_path, {"op": "take_owner", "name": "task.txt", "project": lab_project.code})
    assert not a["ok"] and "inbox/task.txt" in own.objects, (
        "the planted row chose the customer of the take: %s" % {k: a.get(k) for k in ("state", "customer")})


def test_a_row_never_granted_does_not_loosen_the_send_gate_of_the_service(svc, buckets, home, cust_project):
    """Promise (39b9af4): the strict form of the service's send gate follows the owner's record, never the row.
    Attack: a customer project the owner never granted rewrites its row to customer none and puts an unreviewed
    deliverable; the service judges it as a project without a customer and lets it out."""
    lab, _ = buckets
    d = Path(cust_project.path) / "deliverables" / "plan.md"
    d.write_text("The plan: two clusters on managed k8s.\n", encoding="utf-8")
    key = "%s/from-session/2026-10-10/put-0123456789ab.md" % cust_project.code
    req = {"op": "obs", "method": "PUT", "project": cust_project.code, "key": key, "name": "plan.md"}
    a = keys.request(svc.call_path, dict(req), upload=d)
    assert not a["ok"] and key not in lab.objects, "control: the row as spawned is refused"
    _plant_row(home, cust_project.code, projects.NO_CUSTOMER)
    a = keys.request(svc.call_path, dict(req), upload=d)
    assert not a["ok"] and key not in lab.objects, "the planted row loosened the send gate: %s" % a


def test_the_lab_key_of_the_bucket_tenant_under_another_alias_gives_no_lease(svc, monkeypatch):
    """Finding 1, the same key twice: the owner loads the bucket tenant's lab key under a second alias as well; the
    lease compares the key, not only the alias, and mints nothing."""
    from awb.tcp import cloud

    minted = _Minted()
    monkeypatch.setattr(cloud.Client, "request", minted.method())
    store = {"test-1/lab/ak": AK, "test-1/lab/sk": SK, "test-2/lab/ak": AK, "test-2/lab/sk": SK}
    keys.unlock(svc.admin_path, sorted(store), reader=store.get, settings=dict(svc.settings))
    for alias in ("test-1", "test-2"):
        a = keys.request(svc.call_path, {"op": "lease", "tenant": alias, "minutes": 15})
        assert not a["ok"] and "exchange buckets" in a["error"], a
    assert not minted.sent
