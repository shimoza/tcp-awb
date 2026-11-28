"""G10 of the attacker review of 2026-11-28: `awb close` as the work user refuses when the key service does not
answer the question for the kept buckets of the project (locked or stopped), unless --force. Before, it printed one
line and closed, and the bucket of B-094 stayed behind. The vault is a temporary one with invented codes."""
from __future__ import annotations

import sys

from awb import cli, projects
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"


def _work_user_without_key_service(monkeypatch, answer: int = 1):
    asked = []

    def delegate(name, argv):
        asked.append((name, argv))
        return answer

    # the work user for the question of the kept buckets alone: everywhere else it would send the paths of this
    # throw-away Workbench to the host file
    monkeypatch.setattr(cli.config, "is_work_user", lambda: sys._getframe(1).f_code.co_name == "_kept_buckets")
    monkeypatch.setattr(cli, "_delegate", delegate)
    return asked


def _state(home, code):
    return {r.code: r.state for r in projects.load(home)}[code]


def test_the_work_user_cannot_close_while_the_kept_list_cannot_be_read(home, register_path, monkeypatch, capsys):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    asked = _work_user_without_key_service(monkeypatch)
    assert cli._kept_buckets(pr.code, home) is None
    assert cli.main(["close", pr.code]) == 2
    err = capsys.readouterr().err
    assert "could not be checked" in err and "--force" in err
    assert asked and asked[0] == ("lab", ["run", "status", "--project", pr.code])
    assert _state(home, pr.code) == "active"


def test_an_answer_without_a_kept_bucket_still_closes(home, register_path, monkeypatch, capsys):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    _work_user_without_key_service(monkeypatch, answer=0)
    assert cli._kept_buckets(pr.code, home) == []
    assert cli.main(["close", pr.code]) == 0
    assert _state(home, pr.code) == "closed"


def test_force_closes_without_the_answer(home, register_path, monkeypatch, capsys):
    pr = projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)
    _work_user_without_key_service(monkeypatch)
    assert cli.main(["close", pr.code, "--force"]) == 0
    assert "closed %s" % pr.code in capsys.readouterr().out
    assert _state(home, pr.code) == "closed"
