"""K15: the owner mail goes out from the owner's timer, only for allowed commands, only after the name check."""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import config, projects
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"


@pytest.fixture
def proj(home, register_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    return projects.spawn(home, "project", GOAL, fx.CUSTOMER_CODE, register_path)


def _open(proj, *lines):
    with open(Path(proj.path) / "OPEN.md", "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def test_a_command_off_the_allow_list_is_not_mailed(home, proj):
    _open(proj, "- (owner) run `awb cloud call POST /v1/servers`", "- (owner) run `awb gate --selftest`")
    assert projects.mail_owner_commands(home, Path(proj.path), lambda s, m: True) == 0


def test_an_item_with_a_name_is_not_mailed(home, proj):
    _open(proj, "- (owner) run `awb close %s` for %s" % (proj.code, fx.CUSTOMER_FORMS[0]))
    mails = []
    assert projects.mail_owner_commands(home, Path(proj.path), lambda s, m: mails.append(m) or True) == 0
    assert mails == []


def test_the_owner_timer_mails_every_active_project(home, proj):
    _open(proj, "- (owner) run `awb close %s`" % proj.code)
    mails = []
    assert projects.mail_owner_commands_all(home, lambda s, m: mails.append(m) or True) == 1
    assert "awb close %s" % proj.code in mails[0]
    assert projects.mail_owner_commands_all(home, lambda s, m: mails.append(m) or True) == 0
