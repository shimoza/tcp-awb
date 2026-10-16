"""F1 step 11: `awb bucket`, `awb import` and `awb dataset put` reach the owner bucket through the admin socket of the
key service, which holds the one bucket key. A real key service in threads, the stand-in bucket of tests/obs_fake.py,
invented names only."""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

import pytest

import awb
from awb import bucket, cli, jobs, projects
from awb.tcp import keys
from tests import fixtures as fx
from tests.obs_fake import FakeOBS

ROOT = Path(awb.__file__).resolve().parent.parent
AK, SK = "AKLABFAKE", "sk-lab-fake-secret"
GOAL = "move two app clusters to managed k8s"
STORE = {"test-1/lab/ak": AK, "test-1/lab/sk": SK}


@contextlib.contextmanager
def short_dir():
    d = tempfile.mkdtemp(prefix="awbb", dir="/tmp")
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
def own():
    with FakeOBS("awb", AK, SK) as o:
        yield o


@pytest.fixture
def svc(home, own, tmp_path, monkeypatch):
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log",
                         obs_endpoints={own.bucket: own.endpoint})
        s.start()
        monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(s.admin_path))
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        keys.unlock(s.admin_path, sorted(STORE), reader=STORE.get,
                    settings={"bucket_tenant": "test-1", "lab_bucket": "awb-lab", "owner_bucket": own.bucket,
                              "max_mb": "1"})
        try:
            yield s
        finally:
            s.stop()


@pytest.fixture
def project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


def month(project) -> str:
    return project.created[:7]


def test_folder_put_list_and_pull_run_through_the_key_service(svc, own, home, project, tmp_path, capsys):
    base = "%s/%s/" % (month(project), project.code)
    assert cli.main(["bucket", "folder", project.code]) == 0
    assert {base, base + "in/", base + "out/"} <= set(own.objects)
    f = tmp_path / "offer.md"
    f.write_text("offer for %s\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["bucket", "put", project.code, str(f)]) == 0
    assert own.objects[base + "out/offer.md"] == f.read_bytes()
    assert cli.main(["bucket", "put", project.code, str(f)]) == 2
    assert "--replace" in capsys.readouterr().err
    assert cli.main(["bucket", "put", project.code, str(f), "--replace"]) == 0
    own.objects[base + "in/brief.txt"] = b"brief for %s\n" % fx.CUSTOMER_CODE.encode()
    c = bucket.client()
    assert bucket.find_folder(c, project.code) == base
    got = bucket.fetch(home, c, project.code)
    assert got["new"] == 1 and got["files"][0].read_bytes() == own.objects[base + "in/brief.txt"]
    assert cli.main(["bucket", "sync"]) == 0


def test_a_locked_key_service_stops_with_one_line_that_names_unlock(svc, project, capsys):
    keys.request(svc.admin_path, {"op": "lock"})
    for argv in (["bucket", "sync"], ["bucket", "folder", project.code]):
        assert cli.main(argv) == 2
        err = capsys.readouterr().err.strip().splitlines()
        assert len(err) == 1 and "awb keys unlock" in err[0]


def test_a_missing_key_service_stops_with_the_same_line_and_no_key_in_the_environment_helps(home, project, monkeypatch,
                                                                                            capsys):
    monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(home.vault / "nothing.sock"))
    monkeypatch.setenv("AWB_BUCKET_KEYS", "pass:ENTRY")
    assert cli.main(["bucket", "sync"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "awb keys unlock" in err[0]
    assert not hasattr(bucket, "keys_from_reference")


def test_the_key_service_keeps_the_layout_and_the_limit(svc, own, home, project):
    ask = lambda **r: keys.request(svc.admin_path, r)           # noqa: E731
    base = "%s/%s/" % (month(project), project.code)
    other = "%s/%s/" % (month(project), "tcp-zzzz")             # a code that no project holds
    f = home.vault / "a.bin"
    f.write_bytes(b"x")
    big = home.vault / "big.bin"
    big.write_bytes(b"x" * (1024 * 1024 + 1))
    for key in (base + "in/a.txt", base + "x.txt", base + "out/sub/a.txt", other + "out/a.txt", "../out/a.txt",
                "awb/other.txt", base + "out/../in/a.txt"):
        assert keys.request(svc.admin_path, {"op": "bucket_put", "key": key}, upload=f)["ok"] is False, key
    big_answer = keys.request(svc.admin_path, {"op": "bucket_put", "key": base + "out/big.bin"}, upload=big)
    assert big_answer["ok"] is False and "limit" in big_answer["error"]
    assert not [k for k in own.objects if k.endswith(("big.bin", "a.txt", "x.txt", "other.txt"))]
    assert ask(op="bucket_get", key=base + "out/a.txt")["ok"] is False          # a get reads in/ only
    assert ask(op="bucket_list", prefix=other)["ok"] is False
    assert ask(op="bucket_list", prefix="")["ok"] is False                       # the top lists with "/" only
    assert ask(op="bucket_list", prefix="", delimiter="/")["ok"] is True
    assert ask(op="bucket_delete", key=base + "out/a.txt")["error"] == "unknown operation"


def test_other_users_and_the_call_socket_get_nothing(svc, own, project, monkeypatch):
    base = "%s/%s/" % (month(project), project.code)
    own.objects[base + "in/a.txt"] = b"x"
    assert keys.request(svc.call_path, {"op": "bucket_list", "prefix": "", "delimiter": "/"})["ok"] is False
    monkeypatch.setattr(keys.os, "getuid", lambda: 12345)      # the peer is now another user than the service
    answer = keys.request(svc.admin_path, {"op": "bucket_get", "key": base + "in/a.txt"})
    assert answer == {"ok": False, "error": "refused"}


def test_dataset_files_go_through_the_key_service(svc, own):
    c = bucket.client()
    f = Path(svc.admin_path).parent / "z.zip"
    f.write_bytes(b"PK")
    prefix = "datasets/tcp-facts/2026-10-08/"
    assert c.head(prefix + "z.zip") is None
    c.put_file(prefix + "z.zip", f, "application/zip")
    c.put_bytes("datasets/tcp-facts/LATEST", b"2026-10-08\n", "text/plain")
    assert own.objects[prefix + "z.zip"] == b"PK" and own.objects["datasets/tcp-facts/LATEST"] == b"2026-10-08\n"
    assert c.head(prefix + "z.zip")["Content-Length"] == "2"
    with pytest.raises(bucket.BucketError, match="no delete"):
        c.delete(prefix + "z.zip")


def test_move_stops_before_it_changes_anything(svc, own, home, project):
    for k in [k for k in own.objects if project.code in k]:
        del own.objects[k]                                      # the folder spawn made is not the point here
    old = "%s/offer draft/" % month(project)
    own.objects[old + "in/a.pptx"] = b"a"
    with pytest.raises(bucket.BucketError, match="no copy and no delete"):
        bucket.move(home, bucket.client(), old, project.code)
    assert own.objects[old + "in/a.pptx"] == b"a"
