"""awb fetch CODE (P12 of F3): what a project put, downloaded through the key service into one folder of the owner
with the names back.

The key service and two stand-in buckets come from tests/test_xchg.py; every name is an invented fixture name and
every folder a temporary one.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from awb.tcp import fetch
from tests import fixtures as fx
from tests.test_xchg import buckets, cust_project, run, svc  # noqa: F401 (fixtures)


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    """The owner's own terminal: no assistant session in the environment."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


TEXT = "The offer for %s: two clusters, one load balancer.\n" % fx.CUSTOMER_CODE
ID_MD = "put-0123456789ab.md"
ID_BIN = "put-ba9876543210.png"


def _put(lab, code, ident=ID_MD, body=TEXT.encode(), day="2026-10-14"):
    lab.objects["%s/from-session/%s/%s" % (code, day, ident)] = body


def test_a_put_text_is_fetched_with_the_names_back_into_a_private_folder(svc, buckets, cust_project, capsys,
                                                                       tmp_path):
    lab, _ = buckets
    _put(lab, cust_project.code)
    _put(lab, cust_project.code, ID_BIN, b"\x89PNG\r\n\x1a\n\x00\xff")
    into = tmp_path / "fetched"
    code, out, err = run(["fetch", cust_project.code, "--into", str(into)], capsys)
    assert code == 0, err
    named = (into / ID_MD).read_text(encoding="utf-8")
    assert fx.CUSTOMER_FORMS[0] in named and fx.CUSTOMER_CODE not in named
    assert (into / ID_BIN).read_bytes() == b"\x89PNG\r\n\x1a\n\x00\xff"
    assert stat.S_IMODE(os.stat(into).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(into / ID_MD).st_mode) == 0o600
    assert "2 new file(s)" in out and "names put back (1 codes replaced)" in out and "not a text" in out
    assert "folder: %s" % into in out
    fx.assert_no_fixture_name(out + err, "the output of awb fetch")


def test_a_second_fetch_takes_only_what_is_new_and_again_takes_all(svc, buckets, cust_project, capsys, tmp_path):
    lab, _ = buckets
    _put(lab, cust_project.code)
    into = tmp_path / "fetched"
    assert run(["fetch", cust_project.code, "--into", str(into)], capsys)[0] == 0
    code, out, _ = run(["fetch", cust_project.code, "--into", str(into)], capsys)
    assert code == 0 and "0 new file(s)" in out and "1 fetched before" in out and "folder:" not in out
    _put(lab, cust_project.code, "put-aaaaaaaaaaaa.md", day="2026-10-15")
    code, out, _ = run(["fetch", cust_project.code, "--into", str(into)], capsys)
    assert code == 0 and "1 new file(s)" in out and "put-aaaaaaaaaaaa.md" in out
    code, out, _ = run(["fetch", cust_project.code, "--into", str(into), "--again"], capsys)
    assert code == 0 and "2 new file(s)" in out and (into / "put-0123456789ab (2).md").is_file()
    assert "%s/from-session/2026-10-14/%s" % (cust_project.code, ID_MD) in lab.objects, "the put stays"


def test_the_default_folder_is_under_the_owners_home(svc, buckets, cust_project, capsys, tmp_path, monkeypatch):
    lab, _ = buckets
    _put(lab, cust_project.code)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    code, out, err = run(["fetch", cust_project.code], capsys)
    assert code == 0, err
    assert (tmp_path / "home" / "awb-fetch" / cust_project.code / ID_MD).is_file()


def test_a_folder_inside_a_git_tree_or_a_project_is_refused(svc, buckets, cust_project, capsys, tmp_path):
    lab, _ = buckets
    _put(lab, cust_project.code)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    code, _, err = run(["fetch", cust_project.code, "--into", str(tmp_path / "repo" / "out")], capsys)
    assert code == 2 and "git tree" in err
    code, _, err = run(["fetch", cust_project.code, "--into", str(Path(cust_project.path) / "named")], capsys)
    assert code == 2 and ("git tree" in err or "working session" in err)
    assert not (Path(cust_project.path) / "named").exists()


def test_fetch_is_refused_inside_an_assistant_session(svc, buckets, cust_project, capsys, tmp_path, monkeypatch):
    lab, _ = buckets
    _put(lab, cust_project.code)
    monkeypatch.setenv("CLAUDECODE", "1")
    code, _, err = run(["fetch", cust_project.code, "--into", str(tmp_path / "f")], capsys)
    assert code == 2 and "owner side" in err and not (tmp_path / "f").exists()


def test_only_put_ids_under_a_date_folder_are_fetched(svc, buckets, cust_project, capsys, tmp_path):
    lab, _ = buckets
    _put(lab, cust_project.code)
    lab.objects["%s/from-session/loose.md" % cust_project.code] = b"x"
    code, out, _ = run(["fetch", cust_project.code, "--into", str(tmp_path / "f")], capsys)
    assert code == 0 and "1 new file(s)" in out and not (tmp_path / "f" / "loose.md").exists()
    assert fetch.default_folder(cust_project.code).name == cust_project.code
