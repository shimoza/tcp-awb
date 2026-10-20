"""K4 (docs/reviews/REVIEW-2026-10-20.md): a session's project is the one of its own folder, never the one the
request names."""
import os
import socket
from types import SimpleNamespace

import pytest

from awb import projects
from awb.tcp import keys
from tests.test_keys import PROJECT, load, osvc, calls_logged_here, no_waiting  # noqa: F401

OTHER = "tcp-zz99"


@pytest.fixture
def folders(osvc, tmp_path, monkeypatch):
    svc, fake = osvc
    load(svc)
    rows = [SimpleNamespace(code=c, kind="project", state="active", path=str(tmp_path / c)) for c in (PROJECT, OTHER)]
    for r in rows:
        (tmp_path / r.code / "sub").mkdir(parents=True)
    monkeypatch.setattr(projects, "load", lambda p: rows)
    by_code = {r.code: r for r in rows}
    svc._project_row = lambda code: by_code.get(code)
    svc._granted = lambda code: code in by_code
    fake.buckets.setdefault(OTHER + "-lab-1", {"policy": None, "objects": {}})
    return svc, fake, tmp_path, os.getuid() + 1


def _caller(svc, uid):
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        return svc._caller_project(a, uid)
    finally:
        a.close()
        b.close()


def test_the_caller_is_the_project_of_the_folder_the_process_works_in(folders, monkeypatch):
    svc, _fake, root, uid = folders
    monkeypatch.chdir(root / PROJECT / "sub")
    assert _caller(svc, uid) == PROJECT
    monkeypatch.chdir(root / OTHER)
    assert _caller(svc, uid) == OTHER


def test_a_process_outside_every_project_folder_has_no_project(folders, monkeypatch, tmp_path_factory):
    svc, _fake, _root, uid = folders
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    assert _caller(svc, uid) is None


def test_the_owner_is_not_bound_to_a_folder(folders):
    svc, _fake, _root, _uid = folders
    assert _caller(svc, os.getuid()) is keys._NOT_ASKED


def test_a_session_naming_another_project_is_refused_before_anything_is_signed(folders):
    svc, fake, _root, uid = folders
    req = {"tenant": "test-1", "method": "GET", "service": "obs", "path": "/%s-lab-1/a" % OTHER, "project": OTHER}
    with pytest.raises(keys.Refused, match="runs from the folder of an active project"):
        svc._call(req, uid, caller=PROJECT)
    with pytest.raises(keys.Refused):
        svc._call(req, uid, caller=None)
    assert fake.calls == []
    assert svc._call(req, uid, caller=OTHER)["ok"]
