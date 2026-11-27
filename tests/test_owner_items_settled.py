"""B-104: settled items and Steering items leave the owner's list.

One OPEN.md with a real decision, a settled Steering item, a dated open Steering line and a closed section.
Invented codes and hosts only; the vault and the project are temporary folders.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import config, ownerwait, projects
from tests import fixtures as fx

DECISION = "Decide the region for the move. Default: eu-de"
STEERING = "2026-11-24 owner: Steering: allow documentation host b.example.org"
OPEN = "\n".join([
    "## Owner", "",
    "- %s" % DECISION,
    "- [x] Steering: allow documentation host a.example.org",
    "- %s" % STEERING,
    "",
    "## Closed", "",
    "- [x] allow host c.example.org, owner: settled",
    "- owner: a note left under closed",
    "",
]) + "\n"


@pytest.fixture
def world(home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(ownerwait, "winlab_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "winlab_online_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "tenant_mode_items", lambda *a, **k: [])
    proj = projects.spawn(home, "project", "move two app clusters to managed k8s", fx.CUSTOMER_CODE, home.register)
    (Path(proj.path) / "OPEN.md").write_text(OPEN, encoding="utf-8")
    return home, tmp_path / "runs", tmp_path / "none.md"


def test_owner_items_skip_settled_items():
    assert projects._owner_items(OPEN) == [DECISION, STEERING]


def test_a_nested_heading_stays_closed_and_a_same_level_heading_ends_it():
    text = "## Closed\n### Part\n- owner: a\n## Owner\n- b\n"
    assert projects._owner_items(text) == ["b"]


def test_decisions_view_holds_the_decision_only(world):
    home, runs, queue = world
    items, _ = ownerwait.collect(home, queue, runs)
    assert [it.source for it in items if it.kind == "owner"] == [DECISION]
    assert not [it for it in items if "b.example.org" in it.text]


def test_dev_view_holds_the_steering_line(world):
    home, runs, queue = world
    items, _ = ownerwait.collect(home, queue, runs, view="dev")
    assert [it.source for it in items] == [STEERING]


@pytest.mark.parametrize("text", ["[ ] 2026-11-24 owner: allow the documentation host x.example.org",
                                  "owner: Steering: pick a task", "allow documentation host x.example.org"])
def test_dev_start_after_the_lead(text):
    assert ownerwait._DEV_LEAD.sub("", text.lower()).startswith(ownerwait._DEV_START)
