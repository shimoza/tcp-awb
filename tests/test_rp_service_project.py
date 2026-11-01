"""RP: the service project of the weekly fact recheck comes from `recheck_project` of the host file (AWB_CONF here), not
from the runtime; close and delete refuse it and the list marks it. Every code here is invented."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from awb import cli
from awb.tcp import recheck
from tests.lab_fake import Clock
from tests.test_lifecycle import spawn
from tests.test_recheck import PROJECT, READ_BLOCK, Cloud, entry, project  # noqa: F401


def conf(monkeypatch, tmp_path, text):
    f = tmp_path / "paths.conf"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setenv("AWB_CONF", str(f))


def run_without_project():
    cloud = Cloud()
    clock = Clock()
    errors: list[str] = []
    code = recheck.run("read", [], None, "test-1", send=cloud, clock=clock, sleep=clock.sleep,
                       out=lambda line: None, err=errors.append)
    return code, errors, cloud


def test_a_run_takes_the_project_from_the_host_file(project, home, monkeypatch, tmp_path):
    entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK)
    conf(monkeypatch, tmp_path, "recheck_project = %s\n" % PROJECT)
    code, errors, cloud = run_without_project()
    assert code == 0, errors
    assert cloud.calls


def test_the_project_option_wins_over_the_host_file(home, monkeypatch, tmp_path):
    conf(monkeypatch, tmp_path, "recheck_project = tcp-zz2z\n")
    assert recheck.service_project(PROJECT) == PROJECT
    assert recheck.service_project(None) == "tcp-zz2z"


@pytest.mark.parametrize("text", [None, "owner = x\n", "recheck_project =\n"])
def test_a_run_with_neither_stops_with_one_line_that_names_the_key(project, home, monkeypatch, tmp_path, text):
    entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK)
    f = tmp_path / "paths.conf"
    if text is not None:
        f.write_text(text, encoding="utf-8")
    monkeypatch.setenv("AWB_CONF", str(f))
    code, errors, cloud = run_without_project()
    assert code == 2 and not cloud.calls
    assert len(errors) == 1 and "recheck_project" in errors[0] and "\n" not in errors[0]


def test_the_runtime_names_no_service_project():
    text = "".join(f.read_text(encoding="utf-8") for f in Path(recheck.__file__).parent.glob("recheck*.py"))
    assert not re.search(r"tcp-[a-z2-7]{4}\b", text.replace("tcp-q7m4", ""))
    assert not hasattr(recheck, "SERVICE_PROJECT")


def test_close_and_delete_refuse_the_service_project(home, register_path, monkeypatch, tmp_path, capsys):
    pr = spawn(home, register_path)
    other = spawn(home, register_path)
    conf(monkeypatch, tmp_path, "recheck_project = %s\n" % pr.code)
    assert cli.main(["close", pr.code]) == 2
    err = capsys.readouterr().err
    assert "recheck_project" in err and len(err.strip().splitlines()) == 1
    assert cli.main(["close", pr.code, "--force"]) == 0
    capsys.readouterr()
    assert cli.main(["projects", "delete", pr.code]) != 0
    err = capsys.readouterr().err
    assert "recheck_project" in err and "--service" in err
    assert Path(pr.path).exists()
    assert cli.main(["projects", "delete", pr.code, "--service"]) == 0
    assert not Path(pr.path).exists()
    # a project the key does not name is not touched by the rule
    assert cli.main(["close", other.code]) == 0
    assert cli.main(["projects", "delete", other.code]) == 0


def test_list_marks_the_service_project(home, register_path, monkeypatch, tmp_path, capsys):
    pr = spawn(home, register_path)
    other = spawn(home, register_path)
    conf(monkeypatch, tmp_path, "recheck_project = %s\n" % pr.code)
    assert cli.main(["projects", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(x.startswith(pr.code) and "[service project]" in x for x in lines)
    assert not any(x.startswith(other.code) and "[service project]" in x for x in lines)
