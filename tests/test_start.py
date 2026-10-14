"""awb start project|query (P1 of F3): register, spawn, grant and import in one owner command, then the open line.

Every step is the existing command; the spawn as the work user is a stand-in for `sudo -n` that records its argv
and spawns in this process. Every name is an invented fixture name, every folder a temporary one.
"""
from __future__ import annotations

import subprocess

import pytest

from awb import bucket, cli, config, grants, importcmd, projects, register, start
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    """His own terminal on a host without a work user, no bucket key."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


def _lines(capsys) -> str:
    out, err = capsys.readouterr()
    return out + err


def _reader(lines):
    it = iter(lines)
    return lambda: next(it, "")


def test_a_project_of_a_known_customer_is_spawned_granted_and_ends_with_the_open_line(home, capsys):
    assert start.start(home, "project", GOAL, fx.CUSTOMER_CODE) == 0
    (pr,) = projects.load(home)
    assert pr.kind == "project" and pr.customer == fx.CUSTOMER_CODE
    assert grants.load(home)[pr.code]["kind"] == "project" and grants.customer(home, pr.code) == fx.CUSTOMER_CODE
    out = _lines(capsys)
    assert "the bucket folder in/ cannot be read here" in out
    assert out.strip().splitlines()[-1] == "open: awb projects open %s" % pr.code
    fx.assert_no_fixture_name(out, "the output of awb start")


def test_a_new_customer_is_asked_for_its_forms_at_the_prompt_never_as_an_argument(home, capsys):
    assert start.start(home, "query", GOAL, "new", read=_reader([fx.SECOND_COMPANY + "\n", "\n"]), tty=True) == 0
    (pr,) = projects.load(home)
    assert pr.kind == "query" and pr.customer.startswith("CUST-") and pr.customer != fx.CUSTOMER_CODE
    forms = [e.form for e in register.load(home.register) if e.code == pr.customer]
    assert forms == [fx.SECOND_COMPANY]
    assert grants.customer(home, pr.code) == pr.customer
    out = _lines(capsys)
    assert "new customer %s" % pr.customer in out and "Type its written forms" in out
    fx.assert_no_fixture_name(out, "the output of awb start")


def test_without_a_terminal_a_new_customer_is_refused_before_the_spawn(home, capsys):
    assert cli.main(["start", "project", "--goal", GOAL, "--customer", "new"]) == 2
    assert not projects.load(home)
    assert "has no form in the register" in _lines(capsys)


def test_on_a_sealed_host_the_spawn_runs_as_the_work_user_through_sudo(home, capsys, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[-1] == "true":
            return subprocess.CompletedProcess(argv, 0)
        projects.spawn(home, "query", GOAL, None, home.register)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(config, "work_user", lambda: "worker")
    monkeypatch.setattr(projects.shutil, "which", lambda name: "/usr/bin/sudo")
    assert start.start(home, "query", GOAL, None, ["iam"], run=fake_run) == 0
    assert calls[0] == ["/usr/bin/sudo", "-n", "-u", "worker", "true"]
    assert calls[1][:7] == ["/usr/bin/sudo", "-n", "-u", "worker", "-H", "bash", "-lc"]
    assert calls[1][7] == "awb spawn query --goal '%s' --tag iam" % GOAL
    (pr,) = projects.load(home)
    assert "open: awb projects open %s" % pr.code in _lines(capsys)


def test_where_sudo_is_not_allowed_nothing_is_spawned(home, capsys, monkeypatch):
    monkeypatch.setattr(config, "work_user", lambda: "worker")
    monkeypatch.setattr(projects.shutil, "which", lambda name: "/usr/bin/sudo")
    with pytest.raises(projects.ProjectError, match="sudo -n is not allowed"):
        start.start(home, "query", GOAL, None, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1))
    assert not projects.load(home)


def test_what_lies_in_the_folder_in_is_imported(home, capsys, monkeypatch, tmp_path):
    f = tmp_path / "task.txt"
    f.write_text("Migrate the database cluster of %s.\n" % fx.CUSTOMER_FORMS[0], encoding="utf-8")
    monkeypatch.setattr(importcmd, "_from_bucket", lambda p, code: [f])
    assert start.start(home, "project", GOAL, fx.CUSTOMER_CODE) == 0
    out = _lines(capsys)
    assert "files taken: 1" in out and "copies written: 1" in out
    assert (home.outbox / fx.CUSTOMER_CODE).is_dir()
    fx.assert_no_fixture_name(out, "the output of awb start")


def test_start_is_refused_inside_an_assistant_session_and_for_a_bad_customer(home, capsys, monkeypatch):
    assert cli.main(["start", "project", "--goal", GOAL, "--customer", "Q7M4"]) == 2
    assert "a customer code reads CUST-XXXX or new" in _lines(capsys)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["start", "project", "--goal", GOAL]) == 2
    assert "owner side" in _lines(capsys) and not projects.load(home)

