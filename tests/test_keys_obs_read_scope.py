"""K1 (docs/review-2026-10-13.md): the OBS reads a session makes through the key service reach its own project only."""
import os
from types import SimpleNamespace

import pytest

from awb.tcp import keys
from tests.test_keys import PROJECT, call, load, osvc, calls_logged_here, no_waiting  # noqa: F401

OTHER = "tcp-zz99"
SERVICE_PROJECT = "tcp-n7e6"
OWN_BUCKET = PROJECT + "-lab-1"
FOREIGN_BUCKET = OTHER + "-lab-1"
FIXED = "an OBS read reaches only a bucket named after the project (%s-...)" % PROJECT


@pytest.fixture
def session(osvc):
    svc, fake = osvc
    load(svc)
    rows = {c: SimpleNamespace(code=c, kind="project") for c in (PROJECT, SERVICE_PROJECT)}
    svc._project_row = lambda code: rows.get(code)
    svc._granted = lambda code: code in rows
    for b in (OWN_BUCKET, FOREIGN_BUCKET, SERVICE_PROJECT + "-x"):
        fake.buckets.setdefault(b, {"policy": None, "objects": {}})
    return svc, fake, os.getuid() + 1


def read(svc, uid, path, method="GET", project=PROJECT, **extra):
    return svc._call(dict({"tenant": "test-1", "method": method, "service": "obs", "path": path,
                           "project": project}, **extra), uid)


def test_a_read_in_a_bucket_of_the_own_project_is_signed(session):
    svc, fake, uid = session
    assert read(svc, uid, "/%s/notes.txt" % OWN_BUCKET)["status"] == 404
    assert read(svc, uid, "/%s/notes.txt" % OWN_BUCKET, "HEAD")["ok"]
    assert read(svc, uid, "/" + OWN_BUCKET, query={"prefix": "a/"})["status"] == 200
    assert [c[2] for c in fake.calls] == ["OBS"] * 3


def test_a_read_in_a_bucket_of_another_project_is_refused_with_one_fixed_line(session):
    svc, fake, uid = session
    for path, method in (("/%s/notes.txt" % FOREIGN_BUCKET, "GET"), ("/%s/notes.txt" % FOREIGN_BUCKET, "HEAD"),
                         ("/" + FOREIGN_BUCKET, "GET")):
        with pytest.raises(keys.Refused) as err:
            read(svc, uid, path, method, query={"prefix": ""})
        assert str(err.value) == FIXED and "notes" not in str(err.value) and OTHER not in str(err.value)
    with pytest.raises(keys.Refused, match="a bucket named after the project"):
        read(svc, uid, "/" + FOREIGN_BUCKET, query={"prefix": OTHER + "/", "marker": "a"})
    assert fake.calls == []                      # refused before anything is signed and sent


def test_the_list_of_the_buckets_and_the_tags_of_a_bucket_stay_for_the_sweep(session):
    svc, fake, uid = session
    assert read(svc, uid, "/")["status"] == 200
    assert read(svc, uid, "/" + FOREIGN_BUCKET, query={"tagging": ""})["status"] in (200, 404)
    with pytest.raises(keys.Refused):            # the tags of an object are an object read
        read(svc, uid, "/%s/notes.txt" % FOREIGN_BUCKET, query={"tagging": ""})


def test_an_exchange_bucket_stays_closed_to_a_session(session):
    svc, fake, uid = session
    for bucket in ("awb", "awb-lab-eu-de"):
        with pytest.raises(keys.Refused, match="awb xchg only"):
            read(svc, uid, "/%s/%s/in/a.md" % (bucket, PROJECT))


def test_the_owner_reads_any_key_of_any_bucket(session):
    svc, fake, _ = session
    owner = os.getuid()
    for path in ("/%s/notes.txt" % FOREIGN_BUCKET, "/" + FOREIGN_BUCKET, "/%s/notes.txt" % OWN_BUCKET):
        assert read(svc, owner, path, project="")["status"] in (200, 404)
    assert len(fake.calls) == 3


def test_the_service_project_reads_as_any_project(session):
    svc, fake, uid = session
    assert read(svc, uid, "/%s-x/a" % SERVICE_PROJECT, project=SERVICE_PROJECT)["status"] == 404
    with pytest.raises(keys.Refused, match=r"tcp-n7e6-\.\.\."):
        read(svc, uid, "/" + OWN_BUCKET, project=SERVICE_PROJECT)
