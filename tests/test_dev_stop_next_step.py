"""B-099: a development session (a checkout of this repository) does not end a turn on its own next step. The stop
hook holds such a reply once; a reply that names a step for him, a stop with `stop_hook_active`, a desk folder and
a project folder go the way they went before."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awb import config, hooks, projects, ux
from tests import fixtures as fx

BLOCKED = "you named your next step: do it now"
NEXT = "Следующее по плану: группа запросов. Твои шаги: нет."


@pytest.fixture
def world(home, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    monkeypatch.setattr(hooks, "_ux_event", lambda *a, **k: seen.append(a))
    return home, seen


def checkout(base: Path) -> Path:
    co = base / "dev-checkout"
    (co / ".git").mkdir(parents=True)
    (co / "awb").mkdir()
    (co / "awb" / "cli.py").write_text("")
    return co


def transcript(base: Path, reply: str) -> str:
    path = base / "transcript.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": "build the queries"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def stop(folder: Path, base: Path, reply: str, capsys, **extra) -> tuple[int, str]:
    data = {"cwd": str(folder), "transcript_path": transcript(base, reply), **extra}
    code = hooks.hook_stop(data)
    return code, capsys.readouterr().err


def test_a_reply_that_ends_on_its_own_next_step_is_held_once(world, tmp_path, capsys):
    _, seen = world
    code, err = stop(checkout(tmp_path), tmp_path, NEXT, capsys)
    assert code == hooks.BLOCK and BLOCKED in err and "awb build wait ID" in err
    assert err.count("\n") == 1
    assert len(seen) == 1 and seen[0][0] == "stop" and seen[0][2] == "held-note"


@pytest.mark.parametrize("reply", [
    "Done.\n\nNext I will check the queue.",
    "Done.\n\nWaiting for the check.\n\nYour steps: none",
    "Готово.\n\nТеперь чиню B-074.",
    "Готово.\n\nНачинаю с парсера.\n\nТвои шаги: нет",
])
def test_the_other_phrases_and_the_steps_line_of_its_own(world, tmp_path, capsys, reply):
    code, err = stop(checkout(tmp_path), tmp_path, reply, capsys)
    assert code == hooks.BLOCK and BLOCKED in err


@pytest.mark.parametrize("reply", [
    "Следующее по плану: группа запросов. Твои шаги: 1. awb vault unlock",
    "Следующее по плану: группа запросов.\n\nYour steps:\n1. awb vault unlock",
    "Done. The tests pass.",
    "Следующее по плану: группа запросов.\n\nThe queue is empty and the tests pass.",
    "The word nexting is no phrase.",
])
def test_a_step_for_him_or_no_phrase_in_the_last_paragraph_passes(world, tmp_path, capsys, reply):
    _, seen = world
    code, err = stop(checkout(tmp_path), tmp_path, reply, capsys)
    assert code == hooks.OK and BLOCKED not in err and seen == []


def test_stop_hook_active_lets_the_next_stop_through(world, tmp_path, capsys):
    code, err = stop(checkout(tmp_path), tmp_path, NEXT, capsys, stop_hook_active=True)
    assert code == hooks.OK and err == ""


def test_a_desk_folder_passes(world, tmp_path, capsys):
    desk = tmp_path / "desk"
    desk.mkdir()
    assert ux.session_kind(str(desk))[0] == "desk"
    code, err = stop(desk, tmp_path, NEXT, capsys)
    assert code == hooks.OK and BLOCKED not in err


def test_a_project_folder_keeps_its_path(world, tmp_path, capsys, monkeypatch):
    home, _ = world
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    code, err = stop(pr.path, tmp_path, NEXT, capsys)
    assert BLOCKED not in err
    plain, plain_err = stop(pr.path, tmp_path, "Done.", capsys)
    assert code == plain and err == plain_err


def test_an_unreadable_transcript_passes(world, tmp_path):
    folder = checkout(tmp_path)
    assert hooks.hook_stop({"cwd": str(folder), "transcript_path": str(tmp_path / "none.jsonl")}) == hooks.OK
