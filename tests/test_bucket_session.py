"""Attacker review, finding R6: an assistant session of the owner's own uid gets no original from the bucket. The
key service of tests/test_bucket_keys.py, the stand-in bucket of tests/obs_fake.py, invented names only."""
from __future__ import annotations

import pytest

from awb import bucket, cli
from awb.tcp import keys
from tests import fixtures as fx
from tests.test_bucket_keys import month, own, project, svc  # noqa: F401 (fixtures)

SESSION = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


@pytest.fixture
def session(monkeypatch):
    """The shell of an assistant session, whatever the shell of the test run is."""
    monkeypatch.setenv(SESSION[0], "1")
    monkeypatch.delenv(SESSION[1], raising=False)


@pytest.fixture
def terminal(monkeypatch):
    """The owner's own terminal."""
    for name in SESSION:
        monkeypatch.delenv(name, raising=False)


def _in_file(own, project) -> tuple[str, bytes]:  # noqa: F811
    key = "%s/%s/in/brief.txt" % (month(project), project.code)
    own.objects[key] = b"brief for %s\n" % fx.CUSTOMER_CODE.encode()
    return key, own.objects[key]


def test_pull_and_move_refuse_inside_a_session_before_the_key_service(svc, own, home, project, session,  # noqa: F811
                                                                      capsys):
    assert cli.main(["bucket", "folder", project.code]) == 0
    _in_file(own, project)
    capsys.readouterr()
    for args in (["pull", project.code], ["move", "%s/drift/" % month(project), project.code]):
        assert cli.main(["bucket", *args]) == 2
        err = capsys.readouterr().err.strip().splitlines()
        assert len(err) == 1 and "assistant session" in err[0] and "own terminal" in err[0]
        assert "brief" not in err[0]
    assert not home.inbox.is_dir() or list(home.inbox.iterdir()) == []
    assert bucket.pulled(home) == set()
    # the other commands are as they were: the originals are the point, not the folder marks
    assert cli.main(["bucket", "sync"]) == 0


def test_a_get_of_a_file_of_in_refuses_inside_a_session_even_when_called_directly(svc, own, home,  # noqa: F811
                                                                                  project, session, tmp_path):
    key, _ = _in_file(own, project)
    dst = tmp_path / "taken.txt"
    with pytest.raises(bucket.BucketError, match="assistant session"):
        bucket.KeyServiceClient().get(key, dst)
    assert not dst.exists()
    with pytest.raises(bucket.BucketError, match="assistant session"):
        bucket.fetch(home, bucket.client(), project.code)
    assert not home.inbox.is_dir() or list(home.inbox.iterdir()) == []
    assert bucket.pulled(home) == set()


def test_the_session_check_comes_before_the_socket(home, project, session, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(home.vault / "nothing.sock"))
    key = "%s/%s/in/brief.txt" % (month(project), project.code)
    with pytest.raises(bucket.BucketError, match="assistant session"):
        bucket.KeyServiceClient().get(key, tmp_path / "taken.txt")
    assert bucket._is_original(key)
    assert bucket._is_original("2026-10/%s/in/deeper/brief.txt" % project.code)
    assert not bucket._is_original("2026-10/%s/out/offer.md" % project.code)
    assert not bucket._is_original("2026-10/%s/in" % project.code)
    assert not bucket._is_original("datasets/tcp-facts/LATEST")
    assert not bucket._is_original(None)


def test_the_same_fetch_runs_in_the_owners_own_terminal(svc, own, home, project, terminal):  # noqa: F811
    assert cli.main(["bucket", "folder", project.code]) == 0
    key, data = _in_file(own, project)
    got = bucket.fetch(home, bucket.client(), project.code)
    assert got["new"] == 1 and got["files"][0].read_bytes() == data
    assert key in {k for k, _ in bucket.pulled(home)}
