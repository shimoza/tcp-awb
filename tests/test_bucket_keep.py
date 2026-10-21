"""BK: every project folder of the owner bucket holds a .keep file in in/ and out/, so that S3 clients which cannot
open an empty folder open it. awb itself ignores the file. Stand-in bucket of tests/obs_fake.py."""
from __future__ import annotations

from pathlib import Path

import pytest

import awb
from awb import bucket, bucket_layout, importcmd, projects
from awb.tcp import keys
from tests import fixtures as fx
from tests.obs_fake import FakeOBS
from tests.test_bucket_keys import month, own, quiet, svc  # noqa: F401  (fixtures of the key service tests)

ROOT = Path(awb.__file__).resolve().parent.parent
GOAL = "move two app clusters to managed k8s"
NOTE = b"a plain note about the migration plan\n"


@pytest.fixture
def fake():
    with FakeOBS() as f:
        yield f


@pytest.fixture
def project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


def test_a_new_folder_gets_a_keep_file_in_in_and_out(home, fake, project):
    folder, created = bucket.ensure_folder(fake.client(), project)
    assert created
    for sub in ("in/", "out/"):
        assert fake.objects[folder + sub + ".keep"] == bucket_layout.KEEP_TEXT
    assert b"ignored by awb" in bucket_layout.KEEP_TEXT


def test_sync_adds_keep_to_an_old_folder_once_and_never_twice(home, fake, project):
    folder = "%s/%s/" % (project.created[:7], project.code)
    for key in (folder, folder + "in/", folder + "out/"):
        fake.objects[key] = b""                                  # a folder made before BK
    c = fake.client()
    bucket.sync(home, c)
    assert {folder + "in/.keep", folder + "out/.keep"} <= set(fake.objects)
    fake.objects[folder + "in/.keep"] = b"edited by hand"
    puts = []
    real = c.put_bytes
    c.put_bytes = lambda key, *a, **k: (puts.append(key), real(key, *a, **k))[1]
    bucket.sync(home, c)
    assert puts == [] and fake.objects[folder + "in/.keep"] == b"edited by hand"


def test_pull_and_fetch_never_take_keep(home, fake, project, monkeypatch):
    folder, _ = bucket.ensure_folder(fake.client(), project)
    c = fake.client()
    got = bucket.fetch(home, c, project.code)
    assert got["new"] == 0 and got["known"] == 0 and got["outside"] == 0 and got["files"] == []
    fake.objects[folder + "in/brief.txt"] = NOTE
    got = bucket.fetch(home, c, project.code, again=True)
    assert got["new"] == 1 and [f.name for f in got["files"]] == ["brief.txt"]
    assert not any(f.name.endswith("keep") for f in home.inbox.iterdir())


def test_import_from_the_bucket_never_takes_keep(home, fake, project, monkeypatch, capsys):
    monkeypatch.setattr(bucket, "client", lambda: fake.client())
    folder, _ = bucket.ensure_folder(fake.client(), project)
    fake.objects[folder + "in/brief.txt"] = NOTE
    assert importcmd.run(home, project.code, []) == 0
    out = capsys.readouterr().out
    assert "1 file(s) from the bucket folder in/ of %s" % project.code in out
    assert not any("keep" in f.name for f in home.inbox.iterdir())


def test_the_key_service_takes_the_keep_files_and_nothing_wider(svc, own, home, project):  # noqa: F811
    base = "%s/%s/" % (month(project), project.code)
    f = home.vault / "keep.txt"
    f.write_bytes(bucket_layout.KEEP_TEXT)
    for key in (base + "in/.keep", base + "out/.keep"):
        assert keys.request(svc.admin_path, {"op": "bucket_put", "key": key}, upload=f)["ok"] is True, key
    assert own.objects[base + "in/.keep"] == bucket_layout.KEEP_TEXT
    for key in (base + ".keep", base + "in/sub/.keep", base + "in/.keeper", base + "in/a.txt",
                base + "out/sub/.keep", "%s/%s/in/.keep" % (month(project), "tcp-zzzz"), "awb/.keep"):
        assert keys.request(svc.admin_path, {"op": "bucket_put", "key": key}, upload=f)["ok"] is False, key
    assert sorted(k for k in own.objects if k.endswith(".keep")) == [base + "in/.keep", base + "out/.keep"]


def test_sync_through_the_key_service_writes_the_keep_files(svc, own, home, project):  # noqa: F811
    folder, _ = bucket.ensure_folder(bucket.client(), project)
    assert own.objects[folder + "in/.keep"] == bucket_layout.KEEP_TEXT
    assert own.objects[folder + "out/.keep"] == bucket_layout.KEEP_TEXT


def test_a_new_folder_is_month_and_code_with_in_and_out_and_keep(home, fake, project):
    """Replaces test_a_new_folder_is_month_and_code_with_in_and_out of tests/test_bucket_layout.py (B-026-1)."""
    folder, created = bucket.ensure_folder(fake.client(), project)
    assert created and folder == bucket_layout.folder_of(project.created[:7], project.code)
    assert set(fake.objects) == {folder, folder + "in/", folder + "out/", folder + "in/.keep", folder + "out/.keep"}
    assert (bucket.IN, bucket.OUT) == (bucket_layout.IN, bucket_layout.OUT) == ("in/", "out/")
