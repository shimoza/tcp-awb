"""One code per piece of work: awb start, awb spawn and awb import with --customer new issue tcp-xxxx and CUST-XXXX
from the same four characters. An existing customer is refused for a new project. Invented names, temporary folders.
"""
from __future__ import annotations

import pytest

from awb import bucket, cli, codes, config, importcmd, intake, projects, start
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


def _same_four(pr) -> bool:
    return pr.customer == "CUST-" + pr.code.split("-", 1)[1].upper()


def test_start_new_issues_the_project_and_the_customer_from_the_same_four_characters(home, capsys):
    typed = iter([fx.SECOND_COMPANY + "\n", "\n"])
    assert start.start(home, "project", GOAL, "new", read=lambda: next(typed, ""), tty=True) == 0
    (pr,) = projects.load(home)
    assert codes.is_project_code(pr.code) and _same_four(pr)
    assert (home.outbox / pr.customer).is_dir()
    assert "new customer %s for project %s" % (pr.customer, pr.code) in capsys.readouterr().out


def test_spawn_new_issues_the_same_pair(home, capsys):
    assert cli.main(["spawn", "query", "--goal", GOAL, "--customer", "new"]) == 0
    (pr,) = projects.load(home)
    assert _same_four(pr) and (home.outbox / pr.customer).is_dir()


def test_a_taken_pair_is_drawn_again_never_one_code_alone(home, monkeypatch):
    first = iter(["tcp-aaaa", "tcp-bbbb"])
    monkeypatch.setattr(codes, "new_project_code", lambda platform, existing=frozenset(): next(first))
    config.make_dir(home.outbox / "CUST-AAAA", 0o750, shared=True)     # the customer half of the first draw is taken
    code, cust = intake.new_pair(home)
    assert (code, cust) == ("tcp-bbbb", "CUST-BBBB")


def test_start_and_spawn_refuse_an_existing_customer_with_one_line(home, capsys):
    assert cli.main(["start", "project", "--goal", GOAL, "--customer", fx.CUSTOMER_CODE]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "every project gets its own code" in err and "--customer new" in err
    assert cli.main(["spawn", "project", "--goal", GOAL, "--customer", fx.CUSTOMER_CODE]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "every project gets its own code" in err
    assert not projects.load(home)


def test_import_new_refuses_a_taken_customer_code_and_draws_no_other(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(config.__file__).rsplit("/awb/", 1)[0])
    lab = projects.spawn(home, "lab", GOAL, None, register_path)
    taken = importcmd.project_customer_code(lab.code)
    config.make_dir(home.outbox / taken, 0o750, shared=True)
    with pytest.raises(importcmd.ImportRefused, match="taken"):
        importcmd.new_customer(home, lab.code)
