"""The grant names its customer and needs it confirmed (n20): `awb projects kind CODE KIND` on a customer project
writes the grant only with `--customer CODE` equal to the customer of the row, because a session can write that row.
Temporary vault, invented codes of tests/fixtures.py only."""
from __future__ import annotations

import pytest

from awb import bucket, cli, config, grants, projects, start
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"
OTHER = "CUST-" + "Z9Z9"


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


def _spawn(home, customer):
    return projects.spawn(home, "query", GOAL, customer, home.register)


def test_kind_without_customer_on_a_customer_project_writes_no_grant_and_prints_the_line(home, capsys):
    pr = _spawn(home, fx.CUSTOMER_CODE)
    assert cli.main(["projects", "kind", pr.code, "project"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == ("grant not written: the row names %s; run awb projects kind %s project --customer %s"
                           % (fx.CUSTOMER_CODE, pr.code, fx.CUSTOMER_CODE))
    assert not grants.granted(home, pr.code)


def test_kind_with_the_right_customer_writes_the_grant(home, capsys):
    pr = _spawn(home, fx.CUSTOMER_CODE)
    assert cli.main(["projects", "kind", pr.code, "project", "--customer", fx.CUSTOMER_CODE]) == 0
    assert grants.granted(home, pr.code) and grants.customer(home, pr.code) == fx.CUSTOMER_CODE
    assert sorted(grants.load(home)[pr.code]) == ["customer", "kind", "time"]


def test_kind_with_another_customer_refuses_in_one_line_and_writes_nothing(home, capsys):
    pr = _spawn(home, fx.CUSTOMER_CODE)
    assert cli.main(["projects", "kind", pr.code, "project", "--customer", OTHER]) == 1
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and OTHER in err
    assert not grants.granted(home, pr.code) and grants.customer(home, pr.code) is None


def test_a_project_without_a_customer_is_granted_as_before(home, capsys):
    pr = _spawn(home, None)
    assert cli.main(["projects", "kind", pr.code, "project"]) == 0
    assert grants.granted(home, pr.code) and grants.customer(home, pr.code) == projects.NO_CUSTOMER


def test_start_grants_with_the_code_it_registered(home, capsys, monkeypatch):
    seen = []
    real = cli._cmd_projects_kind
    monkeypatch.setattr(cli, "_cmd_projects_kind", lambda args, p: seen.append(args.customer) or real(args, p))
    assert start.start(home, "project", GOAL, "new", read=lambda: "", tty=True) == 0
    (pr,) = projects.load(home)
    own = "CUST-" + pr.code.split("-", 1)[1].upper()
    assert seen == [own] and grants.customer(home, pr.code) == own
    with pytest.raises(start.StartError, match="every project gets its own code"):
        start.start(home, "project", GOAL, fx.CUSTOMER_CODE)
