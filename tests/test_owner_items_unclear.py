"""B-107: an owner item he cannot act on is marked unclear.

One OPEN.md with an item that has no Default, no backticked command and no channel word, and items that have one.
Invented codes only; the vault and the project are temporary folders.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import config, ownerwait, projects
from tests import fixtures as fx

UNCLEAR = "owner: model path for translate of the work user"
WITH_DEFAULT = "owner: reply to the mail. Default: send as is"
WITH_COMMAND = "owner: run `awb winlab close winlab1`"
STEERING = "owner: Steering: pick the next task"
OPEN = "\n".join(["## Owner", ""] + ["- %s" % t for t in (UNCLEAR, WITH_DEFAULT, WITH_COMMAND, STEERING)]) + "\n"


@pytest.fixture
def world(home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(ownerwait, "winlab_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "winlab_online_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "tenant_mode_items", lambda *a, **k: [])
    proj = projects.spawn(home, "project", "move two app clusters to managed k8s", fx.CUSTOMER_CODE, home.register)
    (Path(proj.path) / "OPEN.md").write_text(OPEN, encoding="utf-8")
    return home, tmp_path / "runs", tmp_path / "none.md"


def marks(world, view="decisions"):
    home, runs, queue = world
    items, _ = ownerwait.collect(home, queue, runs, view=view)
    return {it.source: it.unclear for it in items}


def test_an_item_with_no_default_command_or_channel_word_is_unclear(world):
    assert marks(world)[UNCLEAR] is True


def test_an_item_with_a_default_or_a_command_is_not_unclear(world):
    got = marks(world)
    assert got[WITH_DEFAULT] is False and got[WITH_COMMAND] is False


@pytest.mark.parametrize("text", ["sign off the plan in the console", "owner: call the vendor", "a meeting is needed",
                                  "open a terminal", "check the `region` value"])
def test_a_channel_word_or_a_backticked_text_is_not_unclear(text):
    assert not ownerwait.is_unclear(text, False)


def test_an_empty_default_is_unclear():
    assert ownerwait.is_unclear("owner: pick a size. Default:", False)


def test_a_steering_item_is_not_unclear(world):
    assert marks(world, "dev") == {STEERING: False}


def test_status_prints_the_item_as_unclear_and_the_others_as_before(world):
    home, runs, queue = world
    items, parked = ownerwait.collect(home, queue, runs)
    lines = ownerwait.render(items, parked)
    mark = "(unclear, the project rewrites it)"
    line = next(ln for ln in lines if UNCLEAR in ln)
    assert line.split("  ")[1:] == [UNCLEAR, mark, "[project session]"]
    assert sum(ln.count(mark) for ln in lines) == 1


def test_the_mail_carries_the_count(world):
    home, runs, queue = world
    sent = []
    word = ownerwait.mail(home, lambda subject, body: sent.append(body) or "sent", queue, runs)
    assert word == "sent"
    assert sent[0].splitlines()[1] == "1 unclear item, the project rewrites it"


def test_the_wait_history_leaves_unclear_items_out(world):
    home, runs, queue = world
    items, _ = ownerwait.collect(home, queue, runs, view="all")
    kept = ownerwait.waiting(items)
    assert [it.source for it in kept if it.kind == "owner"] == [WITH_DEFAULT, WITH_COMMAND]
