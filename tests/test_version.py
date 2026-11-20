"""UXR9: awb version reads the live release from the deploy journal or the installed source link (tmp paths only)."""
from __future__ import annotations

import json
import os

import pytest

from awb import cli, version

A = "a" * 40
B = "b" * 40
C = "c" * 40


@pytest.fixture
def opt(tmp_path, monkeypatch):
    root = tmp_path / "opt"
    (root / "releases" / A).mkdir(parents=True)
    monkeypatch.setattr(version, "JOURNAL", root / "DEPLOYED")
    monkeypatch.setattr(version, "SRC", root / "src")
    return root


def journal(root, **data):
    base = dict(state="done", target=A, previous=B, commits=[A, B], pending=[], notes_ready=True,
                finished="2026-11-20T07:01:34Z")
    base.update(data)
    (root / "DEPLOYED").write_text(json.dumps(base))


def run(capsys, *argv):
    capsys.readouterr()
    code = cli.main(["version", "live", *argv])
    return code, capsys.readouterr().out


def test_live_release_from_the_journal(opt, capsys):
    journal(opt)
    os.symlink("releases/" + A, opt / "src")
    code, out = run(capsys)
    assert code == 0 and out == "live release aaaaaaa (%s), deployed 2026-11-20T07:01:34Z\n" % A


def test_commit_live_or_not_yet(opt, capsys):
    journal(opt)
    assert run(capsys, "bbbbbbb") == (0, "live release aaaaaaa (%s), deployed 2026-11-20T07:01:34Z\n"
                                        "bbbbbbbbbbbb is live in release aaaaaaa\n" % A)
    code, out = run(capsys, C)
    assert code == 1 and out.endswith("cccccccccccc is not live yet (release aaaaaaa)\n")
    assert run(capsys, "XYZ")[0] == 2


def test_running_deploy_names_the_previous_release_and_trusts_no_commit(opt, capsys):
    journal(opt, state="running", target=C, commits=[C, A, B], finished=None)
    code, out = run(capsys, C)
    assert code == 1
    assert out.splitlines() == ["live release bbbbbbb (%s); the last deploy of ccccccc is running" % B,
                                "cccccccccccc: not known to be live (no finished deploy recorded its commits)"]


def test_without_journal_the_source_link_and_without_both_unknown(opt, capsys):
    os.symlink("releases/" + A, opt / "src")
    code, out = run(capsys)
    assert code == 0 and "live release aaaaaaa" in out and "installed source link" in out
    assert run(capsys, "aaaaaaa")[0] == 0
    (opt / "src").unlink()
    assert run(capsys) == (1, "live release unknown: no deploy journal and no installed source link\n")


def test_source_link_that_differs_from_the_journal_is_named(opt, capsys):
    journal(opt)
    (opt / "releases" / B).mkdir()
    os.symlink("releases/" + B, opt / "src")
    assert "the installed source link names bbbbbbb" in run(capsys)[1]


def test_version_without_a_subcommand_is_a_usage_error(opt, capsys):
    journal(opt)
    assert cli.main(["version"]) == 2
