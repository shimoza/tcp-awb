"""B-108: a held stop names its source in events.tsv (the tokens of the messages that held, joined by "+") and
`awb ux report` counts the tokens under the table."""
from __future__ import annotations

import datetime as dt

import pytest

from awb import cli, config, hooks, opensweep, projects, ux
from awb import status as _status
from tests import fixtures as fx

UTC = dt.timezone.utc
FRIDAY = dt.datetime(2026, 11, 20, 9, 30, tzinfo=UTC)


@pytest.fixture
def world(home, monkeypatch):
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    monkeypatch.setattr(ux, "now", lambda: FRIDAY)
    monkeypatch.setattr(hooks, "draft_ledger", lambda *a, **k: False)
    monkeypatch.setattr(hooks, "turn_prompt", lambda transcript: None)
    monkeypatch.setattr(hooks, "draft_gate", lambda data, root: [])
    monkeypatch.setattr(projects, "mail_owner_commands", lambda *a, **k: None)
    monkeypatch.setattr(_status, "reminder", lambda root: None)
    monkeypatch.setattr(opensweep, "reminder", lambda root: None)
    return home, pr


def rows(home) -> list[list[str]]:
    path = home.shared / "ux" / "events.tsv"
    return [ln.split("\t") for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_a_stop_held_by_the_late_status_and_the_open_sweep_writes_both_tokens(world, monkeypatch, capsys):
    home, pr = world
    monkeypatch.setattr(_status, "reminder", lambda root: "the status is late")
    monkeypatch.setattr(opensweep, "reminder", lambda root: "an open item waits")
    assert hooks.hook_stop({"cwd": str(pr.path), "session_id": "b108"}) == hooks.BLOCK
    capsys.readouterr()
    assert [(r[4], r[5]) for r in rows(home)] == [("held-note", "late+open")]


def test_a_held_draft_names_the_drafts(world, monkeypatch, capsys):
    home, pr = world
    monkeypatch.setattr(hooks, "draft_gate", lambda data, root: ["run awb write check on the draft"])
    assert hooks.hook_stop({"cwd": str(pr.path), "session_id": "b108"}) == hooks.BLOCK
    capsys.readouterr()
    assert [(r[4], r[5]) for r in rows(home)] == [("held-draft", "drafts")]


def test_a_prompt_line_keeps_the_dash(world):
    home, pr = world
    assert ux.hook_event("prompt", {"prompt": "go on", "cwd": str(pr.path)}, None, "late") == "nudge"
    assert [(r[4], r[5]) for r in rows(home)] == [("nudge", "-")]


def test_a_token_outside_the_list_is_refused(world):
    home, pr = world
    with pytest.raises(ux.UxError):
        ux.hook_event("stop", {"cwd": str(pr.path)}, "held-note", ["late", "mood"])
    assert rows(home) == []
    assert ux.hook_event("stop", {"cwd": str(pr.path)}, "held-note", ["next-step"]) == "held-note"
    assert rows(home)[0][5] == "next-step"


def test_the_report_counts_the_tokens_over_two_lines(world, capsys):
    home, pr = world
    for text in ("late+open", "late", "-"):
        ux.append(ux.line("held-note", "project", "tcp-q7m4", "work", text, FRIDAY))
    ux.append(ux.line("nudge", "project", "tcp-q7m4", "work", "late", FRIDAY))
    assert cli.main(["ux", "report", "--since", "2026-11-02"]) == 0
    out = capsys.readouterr().out.splitlines()
    held = [ln for ln in out if ln.startswith("held by ")]
    assert held == ["held by late: 2", "held by open: 1"]
