"""The owner's grant of one existing bucket to one project (ask 2026-11-27-tcp-7c7b-2): awb/tcp/bucketgrant.py, the
OBS read of the key service and awb lab obs."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from awb import config
from awb.tcp import bucketgrant, keys, labobs, tenants
from tests.test_keys import PROJECT, load, osvc, calls_logged_here, no_waiting  # noqa: F401
from tests import test_lo_lab_obs as lo
from tests.test_lo_lab_obs import world  # noqa: F401

OTHER = "tcp-zz99"
TENANT = "tcp-10503"
BUCKET = "shared-images"
WORD = "the bucket shared-images belongs only to " + PROJECT
DAY = "2026-11-27"
EXCHANGE = {"awb", "awb-lab-eu-de"}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    return SimpleNamespace(vault=tmp_path / "vault")


def give(p, tenant=TENANT, bucket=BUCKET, code=PROJECT, word=WORD, day=DAY):
    return bucketgrant.grant(p, tenant, bucket, code, word, day, say=lambda _line: None, exchange=EXCHANGE)


# --------------------------------------------------------------------------- the file of the owner


def test_a_grant_is_written_to_the_vault_with_the_owner_s_word_and_the_day(vault):
    assert give(vault) is True
    f = bucketgrant.path(vault)
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert json.loads(f.read_text(encoding="utf-8")) == [
        {"tenant": TENANT, "bucket": BUCKET, "project": PROJECT, "word": WORD, "day": DAY}]
    assert bucketgrant.holder(vault, TENANT, BUCKET) == PROJECT
    assert give(vault) is False                                   # granted already
    assert bucketgrant.list_lines(vault) == ['%s %s %s: owner\'s word: "%s", %s' % (TENANT, BUCKET, PROJECT, WORD,
                                                                                    DAY)]


def test_the_old_tenant_name_is_taken_and_written_as_the_new_one(vault):
    said = []
    assert bucketgrant.grant(vault, "test-10503", BUCKET, PROJECT, WORD, DAY, say=said.append, exchange=EXCHANGE)
    assert said and "test-10503 is the old name of tcp-10503" in said[0]
    assert json.loads(bucketgrant.path(vault).read_text(encoding="utf-8"))[0]["tenant"] == TENANT
    assert bucketgrant.holder(vault, "test-10503", BUCKET) == PROJECT
    assert bucketgrant.holder(vault, TENANT, BUCKET) == PROJECT
    assert bucketgrant.holder(vault, "tcp-10491", BUCKET) is None    # another tenant


def test_a_bucket_goes_to_one_project_alone(vault):
    give(vault)
    with pytest.raises(bucketgrant.BucketGrantError, match="granted to another project"):
        give(vault, code=OTHER)
    assert bucketgrant.holder(vault, TENANT, BUCKET) == PROJECT
    assert bucketgrant.revoke(vault, TENANT, BUCKET, say=lambda _l: None) is True
    assert bucketgrant.holder(vault, TENANT, BUCKET) is None
    assert give(vault, code=OTHER) is True


@pytest.mark.parametrize("tenant, bucket, code, word, day, why", [
    ("tcp-ab2c", BUCKET, PROJECT, WORD, DAY, "tenant tcp-NNNNN"),
    (TENANT, "Shared_Images", PROJECT, WORD, DAY, "3 to 63"),
    (TENANT, BUCKET, "customer", WORD, DAY, "project code"),
    (TENANT, PROJECT + "-lab-1", PROJECT, WORD, DAY, "needs no grant"),
    (TENANT, OTHER + "-data", PROJECT, WORD, DAY, "needs no grant"),
    (TENANT, "awb-lab-eu-de", PROJECT, WORD, DAY, "awb xchg only"),
    (TENANT, BUCKET, PROJECT, "  ", DAY, "empty"),
    (TENANT, BUCKET, PROJECT, WORD, "27.11.2026", "YYYY-MM-DD"),
    (TENANT, BUCKET, PROJECT, WORD, "2026-13-01", "YYYY-MM-DD"),
])
def test_a_grant_that_is_not_well_formed_is_refused_and_nothing_is_written(vault, tenant, bucket, code, word, day,
                                                                             why):
    with pytest.raises(bucketgrant.BucketGrantError, match=why):
        give(vault, tenant, bucket, code, word, day)
    assert not bucketgrant.path(vault).exists()


def test_the_work_user_cannot_grant(vault, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    with pytest.raises(bucketgrant.BucketGrantError, match="the owner's"):
        give(vault)
    assert not bucketgrant.path(vault).exists()


@pytest.mark.parametrize("text", [
    "{not json",
    '{"tenant": "tcp-10503"}',
    '[{"tenant": "tcp-10503", "bucket": "shared-images", "project": "tcp-ab2c", "word": "x"}]',
    '[{"tenant": "tcp-10503", "bucket": "shared-images", "project": "tcp-ab2c", "word": "x", "day": "2026-11-27"},'
    ' {"tenant": "tcp-10503", "bucket": "other-images", "project": "TCP-AB2C", "word": "x", "day": "2026-11-27"}]',
    '[{"tenant": "tcp-10503", "bucket": "shared-images", "project": "tcp-ab2c", "word": "x", "day": "2026-11-27"},'
    ' {"tenant": "tcp-10503", "bucket": "shared-images", "project": "tcp-zz99", "word": "x", "day": "2026-11-27"}]',
    '[{"tenant": "tcp-10503", "bucket": "tcp-zz99-x", "project": "tcp-ab2c", "word": "x", "day": "2026-11-27"}]',
])
def test_a_broken_file_grants_nothing_and_is_never_overwritten(vault, text):
    vault.vault.mkdir(parents=True)
    bucketgrant.path(vault).write_text(text, encoding="utf-8")
    assert bucketgrant.load(vault) == []
    assert bucketgrant.holder(vault, TENANT, BUCKET) is None
    with pytest.raises(bucketgrant.BucketGrantError, match="broken"):
        give(vault, bucket="new-images")
    assert bucketgrant.path(vault).read_text(encoding="utf-8") == text


def test_a_missing_file_grants_nothing(vault):
    assert bucketgrant.load(vault) == [] and bucketgrant.holder(vault, TENANT, BUCKET) is None
    assert bucketgrant.list_lines(vault) == ["no bucket grant"]


def test_the_command_grants_lists_and_revokes(vault, monkeypatch, capsys):
    monkeypatch.setattr(config, "paths", lambda: vault)
    monkeypatch.setattr(bucketgrant, "_exchange_buckets", lambda: EXCHANGE)
    assert tenants.main(["bucket-grant", "test-10503", BUCKET, PROJECT, "--owner-word", WORD, "--day", DAY]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "%s owns %s on %s: granted" % (PROJECT, BUCKET, TENANT)
    assert "old name of tcp-10503" in out.err
    assert tenants.main(["bucket-grants"]) == 0
    assert WORD in capsys.readouterr().out
    assert tenants.main(["bucket-grant", TENANT, BUCKET, OTHER, "--owner-word", "x"]) == 1
    assert "another project" in capsys.readouterr().err
    assert tenants.main(["bucket-revoke", TENANT, BUCKET]) == 0
    assert capsys.readouterr().out.strip() == "%s: revoked" % BUCKET
    assert bucketgrant.load(vault) == []


# --------------------------------------------------------------------------- the OBS read of the key service


@pytest.fixture
def session(osvc, tmp_path, monkeypatch):
    svc, fake = osvc
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    p = SimpleNamespace(vault=tmp_path / "grant-vault")
    svc.paths_fn = lambda: p
    with svc.lock:
        svc.tenants[TENANT] = svc.tenants["test-1"]
        svc.tenants["test-10503"] = svc.tenants["test-1"]
    svc._registered = lambda: {TENANT: ("eu-de",), "test-10503": ("eu-de",), "test-1": ("eu-de",)}
    rows = {c: SimpleNamespace(code=c, kind="project") for c in (PROJECT, OTHER)}
    svc._project_row = lambda code: rows.get(code)
    svc._granted = lambda code: code in rows
    for b in (BUCKET, "public-images"):
        fake.buckets.setdefault(b, {"policy": None, "objects": {}})
    return svc, fake, os.getuid() + 1, p


def read(svc, uid, path, project=PROJECT, tenant=TENANT, **extra):
    return svc._call(dict({"tenant": tenant, "method": "GET", "service": "obs", "path": path, "project": project},
                          **extra), uid)


def test_a_granted_bucket_is_read_by_its_project_on_both_tenant_names(session):
    svc, fake, uid, p = session
    give(p)
    for tenant in (TENANT, "test-10503"):
        assert read(svc, uid, "/" + BUCKET, tenant=tenant, query={"max-keys": "5", "prefix": "a/"})["status"] == 200
        assert read(svc, uid, "/%s/notes.txt" % BUCKET, tenant=tenant)["status"] == 404
    assert len(fake.calls) == 4


def test_a_granted_bucket_is_refused_for_every_other_project_its_tags_included(session):
    svc, fake, uid, p = session
    give(p)
    for path, query in (("/" + BUCKET, None), ("/%s/notes.txt" % BUCKET, None), ("/" + BUCKET, {"tagging": ""})):
        with pytest.raises(keys.Refused) as err:
            read(svc, uid, path, project=OTHER, query=query)
        assert str(err.value) == bucketgrant.REFUSED_LINE % BUCKET
    assert fake.calls == []


def test_without_a_grant_the_bucket_stays_refused_and_a_grant_on_another_tenant_does_not_count(session):
    svc, fake, uid, p = session
    fixed = "an OBS read reaches only a bucket named after the project (%s-...)" % PROJECT
    with pytest.raises(keys.Refused) as err:
        read(svc, uid, "/" + BUCKET)
    assert str(err.value) == fixed
    give(p, tenant="tcp-10491")
    with pytest.raises(keys.Refused) as err:
        read(svc, uid, "/" + BUCKET)
    assert str(err.value) == fixed
    give(p, bucket="other-images")
    with pytest.raises(keys.Refused):
        read(svc, uid, "/public-images")                          # an ungranted foreign bucket
    assert fake.calls == []


def test_a_broken_grants_file_grants_nothing_in_the_key_service(session):
    svc, fake, uid, p = session
    p.vault.mkdir(parents=True)
    bucketgrant.path(p).write_text("[{broken", encoding="utf-8")
    with pytest.raises(keys.Refused, match="named after the project"):
        read(svc, uid, "/" + BUCKET)
    assert fake.calls == []


def test_a_grant_opens_no_obs_write_of_a_call(session):
    svc, fake, uid, p = session
    give(p)
    with pytest.raises(keys.Refused, match="an OBS write reaches only a bucket named after the project"):
        svc._call({"tenant": TENANT, "role": "lab", "method": "PUT", "service": "obs",
                   "path": "/" + BUCKET, "project": PROJECT}, uid)
    assert fake.calls == []


def test_the_lease_policy_keeps_the_buckets_named_after_the_project(session):
    svc, _fake, _uid, p = session
    give(p)
    text = json.dumps(svc.lease_policy(PROJECT))
    assert BUCKET not in text and "bucket:%s-*" % PROJECT in text


# --------------------------------------------------------------------------- awb lab obs


def lab_grant(w, code=lo.PROJECT, bucket=BUCKET):
    """A grant on the lab tenant of the lab obs tests (test-10491, the old name of tcp-10491)."""
    give(w.svc.paths_fn(), tenant="tcp-10491", bucket=bucket, code=code)
    w.fake.buckets.setdefault(bucket, {"objects": {}, "uploads": {}})


def test_lo_a_put_reaches_a_bucket_granted_to_the_project(world):
    w = world
    lab_grant(w)
    run = lo.start_run(w)
    path, data = lo.a_file(w)
    a = lo.lab_obs(w, "put", run, path, bucket=BUCKET)
    assert a["ok"], a
    job = lo.wait(w, a["job"])
    assert job["state"] == "done", job
    assert w.fake.buckets[BUCKET]["objects"][lo.KEY]["data"] == data


def test_lo_a_bucket_granted_to_another_project_is_refused(world):
    w = world
    lab_grant(w, code=lo.OTHER)
    run = lo.start_run(w)
    path, _data = lo.a_file(w)
    a = lo.lab_obs(w, "put", run, path, bucket=BUCKET)
    assert not a["ok"] and a["error"] == "refused: the bucket %s is granted to another project" % BUCKET, a
    assert w.fake.calls == [] and labobs.load_jobs(w.svc.paths_fn()) == []


def test_lo_an_ungranted_bucket_stays_refused_beside_a_grant(world):
    w = world
    lab_grant(w)
    run = lo.start_run(w)
    path, _data = lo.a_file(w)
    a = lo.lab_obs(w, "put", run, path, bucket="public-images")
    assert not a["ok"] and "only a bucket named after the project (%s-...)" % lo.PROJECT in a["error"], a
    assert labobs.load_jobs(w.svc.paths_fn()) == []
