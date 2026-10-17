"""P8 and P13 of F3: a mail when OPEN.md holds an owner item with a command, and a close that lists what is left.

Invented names only; the mail is a stand-in list and the project a temporary folder.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import cli, config, hooks, projects
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"


@pytest.fixture
def proj(home, register_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    return projects.spawn(home, "project", GOAL, fx.CUSTOMER_CODE, register_path)


def _open(proj, *lines):
    with open(Path(proj.path) / "OPEN.md", "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def test_an_owner_item_with_a_command_is_mailed_once(home, proj):
    _open(proj, "- Switch to a project for the live test: awb projects kind %s project (owner)" % proj.code,
          "- Which region holds the backups?")
    mails = []
    send = lambda s, m: mails.append((s, m)) or True
    assert projects.mail_owner_commands(home, Path(proj.path), send) == 1
    assert projects.mail_owner_commands(home, Path(proj.path), send) == 0
    assert len(mails) == 1
    subject, body = mails[0]
    assert proj.code in subject and "awb projects kind %s project" % proj.code in body
    assert "backups" not in body
    fx.assert_no_fixture_name(subject + body, "the owner mail")


def test_a_mail_that_failed_is_tried_again_and_a_new_item_is_mailed(home, proj):
    _open(proj, "- (owner) run `awb keys unlock`")
    assert projects.mail_owner_commands(home, Path(proj.path), lambda s, m: False) == 0
    mails = []
    assert projects.mail_owner_commands(home, Path(proj.path), lambda s, m: mails.append(m) or True) == 1
    _open(proj, "- (owner) run `awb vault unlock`")
    assert projects.mail_owner_commands(home, Path(proj.path), lambda s, m: mails.append(m) or True) == 1
    assert len(mails) == 2 and mails[0].rstrip().endswith("awb keys unlock")


def test_an_item_without_a_clean_command_is_not_mailed(home, proj):
    _open(proj, "- (owner) run `awb cloud call; rm x`", "- (owner) decide the region")
    assert projects.mail_owner_commands(home, Path(proj.path), lambda s, m: True) == 0


def test_the_stop_hook_sends_the_mail(home, proj, monkeypatch):
    _open(proj, "- (owner) run `awb keys unlock`")
    mails = []
    from awb.tcp import keys
    monkeypatch.setattr(keys, "send_mail", lambda s, m: mails.append(s) or "sent")
    monkeypatch.setattr(hooks, "config", config, raising=False)
    monkeypatch.setattr(config, "paths", lambda: home)
    hooks.hook_stop({"cwd": str(proj.path)})
    assert len(mails) == 1


def test_close_lists_the_open_items_and_live_rows_when_it_refuses(home, proj, capsys):
    _open(proj, "- (owner) run `awb keys unlock`", "- Which region holds the backups?")
    with open(Path(proj.path) / "RESOURCES.md", "a", encoding="utf-8") as f:
        f.write("| ecs-3 | %s | server | eu-de | small | 2026-10-20 | live | |\n" % ("a" * 32))
    assert cli.main(["close", proj.code]) == 2
    err = capsys.readouterr().err
    assert "open: (owner) run `awb keys unlock` (run: awb keys unlock)" in err
    assert "open: Which region holds the backups?" in err
    assert "live: ecs-3 (server, live) (run: awb cloud sweep)" in err
    assert "a" * 32 not in err
    fx.assert_no_fixture_name(err, "the close refusal")
