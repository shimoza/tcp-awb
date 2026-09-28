"""awb/bucket.py against a local stand-in bucket (tests/obs_fake.py) and a throw-away Workbench."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

import awb
from awb import bucket, cli, config, obs, projects
from tests import fixtures as fx
from tests.obs_fake import FakeOBS
from tests.test_obs import fake_pass

ROOT = Path(awb.__file__).resolve().parent.parent
GOAL = "move two app clusters to managed k8s"


@pytest.fixture
def fake():
    with FakeOBS() as f:
        yield f


@pytest.fixture
def project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


def test_a_missing_folder_is_created_under_the_month_of_the_project(home, fake, project):
    c = fake.client()
    folder, created = bucket.ensure_folder(c, project)
    assert created and folder == "%s/%s/" % (project.created[:7], project.code)
    assert {folder, folder + "in/", folder + "out/"} <= set(fake.objects)
    again, created = bucket.ensure_folder(c, project)
    assert again == folder and not created


def test_a_folder_he_made_under_another_month_is_used_where_it_is(home, fake, project):
    fake.objects["2026-01/%s/in/brief.pptx" % project.code] = b"x"
    folder, created = bucket.ensure_folder(fake.client(), project)
    assert folder == "2026-01/%s/" % project.code and not created
    assert "2026-01/%s/out/" % project.code in fake.objects


def test_two_months_for_one_project_are_refused(home, fake, project):
    for month in ("2026-01", "2026-02"):
        fake.objects["%s/%s/in/a.txt" % (month, project.code)] = b"x"
    with pytest.raises(bucket.BucketError, match="more than one month"):
        bucket.find_folder(fake.client(), project.code)


def test_sync_creates_folders_and_reports_drift_without_its_name(home, fake, project):
    fake.objects["2026-09/%s offer/in/a.txt" % fx.CUSTOMER_FORMS[1]] = b"x"   # named after the customer by hand
    text = "\n".join(bucket.sync(home, fake.client()))
    assert project.code in text and "created" in text
    assert "2026-09  1 folder(s) match no project" in text
    fx.assert_no_fixture_name(text, "the output of sync")


def test_pull_takes_new_files_into_the_intake_once(home, fake, project):
    folder, _ = bucket.ensure_folder(fake.client(), project)
    fake.objects[folder + "in/notes %s.txt" % fx.CUSTOMER_FORMS[1]] = (
        "umsetzung mit terraform fuer %s.\n" % fx.CUSTOMER_FORMS[0]).encode("utf-8")
    fake.objects[folder + "stray.txt"] = b"left alone"
    r = bucket.pull(home, fake.client(), project.code)
    assert (r["new"], r["known"], r["outside"]) == (1, 0, 1)
    res = r["intake"]
    assert not res.blocked and len(res.outputs) == 1
    fx.assert_no_fixture_name(res.outputs[0].read_text(encoding="utf-8"), "an output of the pull")
    record = home.vault / bucket.PULLED
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    again = bucket.pull(home, fake.client(), project.code)
    assert (again["new"], again["known"], again["intake"]) == (0, 1, None)


def test_pull_needs_a_customer_and_a_folder(home, fake, register_path, monkeypatch, project):
    lab = projects.spawn(home, "lab", GOAL, None, register_path)
    with pytest.raises(bucket.BucketError, match="no customer"):
        bucket.pull(home, fake.client(), lab.code)
    with pytest.raises(bucket.BucketError, match="no folder"):
        bucket.pull(home, fake.client(), project.code)
    with pytest.raises(bucket.BucketError, match="not a registered project"):
        bucket.pull(home, fake.client(), "tcp-zzzz")


def test_put_writes_to_out_and_keeps_an_existing_file(home, fake, project, tmp_path):
    f = tmp_path / "offer.md"
    f.write_text("offer for %s\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    key, stats = bucket.put(home, fake.client(), project.code, f)
    assert key.endswith("%s/out/offer.md" % project.code) and stats is None
    assert fake.objects[key] == f.read_bytes()
    with pytest.raises(bucket.BucketError, match="--replace"):
        bucket.put(home, fake.client(), project.code, f)
    bucket.put(home, fake.client(), project.code, f, replace=True)


def test_put_with_reveal_uploads_the_named_text_only(home, fake, project, tmp_path):
    f = tmp_path / "offer.md"
    f.write_text("offer for %s\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    key, stats = bucket.put(home, fake.client(), project.code, f, reveal=True)
    assert fake.objects[key].decode("utf-8") == "offer for %s\n" % fx.CUSTOMER_FORMS[0]
    assert f.read_text(encoding="utf-8") == "offer for %s\n" % fx.CUSTOMER_CODE
    assert stats["replaced"] == 1
    assert not any((home.vault / "tmp").iterdir()), "the named temporary file is gone"


def test_move_renames_a_drifted_folder_by_copy_check_and_delete(home, fake, project):
    old = "2026-09/offer draft/"
    for name in ("in/a.pptx", "in/b.pdf", "out/c.md"):
        fake.objects[old + name] = name.encode()
    r = bucket.move(home, fake.client(), old, project.code)
    assert r == {"moved": 3, "target": "2026-09/%s/" % project.code}
    assert not any(k.startswith(old) for k in fake.objects)
    assert fake.objects["2026-09/%s/in/a.pptx" % project.code] == b"in/a.pptx"


def test_move_deletes_nothing_when_the_copy_does_not_match(home, fake, project, monkeypatch):
    old = "2026-09/offer draft/"
    fake.objects[old + "in/a.pptx"] = b"12345"
    monkeypatch.setattr(obs.Client, "copy", lambda self, source, target: self.put_bytes(target, b"1"))
    with pytest.raises(bucket.BucketError, match="nothing was deleted"):
        bucket.move(home, fake.client(), old, project.code)
    assert fake.objects[old + "in/a.pptx"] == b"12345"


def test_the_work_user_cannot_use_the_bucket(monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    with pytest.raises(bucket.BucketError, match="owner's"):
        bucket.settings()


def test_the_command_line_end_to_end(home, fake, project, tmp_path, monkeypatch, capsys):
    fake_pass(tmp_path, monkeypatch, {"cloud/owner/ak": fake.ak, "cloud/owner/sk": fake.sk})
    monkeypatch.setenv("AWB_BUCKET_KEYS", "pass:cloud/owner")
    monkeypatch.setenv("AWB_BUCKET_ENDPOINT", fake.endpoint)
    monkeypatch.setenv("AWB_BUCKET", fake.bucket)
    assert cli.main(["bucket", "folder", project.code]) == 0
    assert project.code in capsys.readouterr().out
    assert cli.main(["bucket", "sync"]) == 0
    capsys.readouterr()
    monkeypatch.delenv("AWB_BUCKET_KEYS")
    assert cli.main(["bucket", "sync"]) == 2
    assert "AWB_BUCKET_KEYS" in capsys.readouterr().err
