"""UXR9: project to desk asks with the mechanics of the dev asks, the desk list and close, the owner's word stamp."""
from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awb import cli, config, deskask, devask, hooks, ownerword, projects, rulesync
from tests import fixtures as fx

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def world(home, monkeypatch):
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.chdir(pr.path)
    return home, pr


def ask(world, *options, text="two DNS records for the lab"):
    home, _ = world
    assert cli.main(["desk", "ask", text, *options]) == 0
    return sorted((home.shared / "notices/desk").glob("*.md"))[-1]


def prompt(monkeypatch, capsys, cwd, sid="uxr9-desk"):
    monkeypatch.setattr(hooks, "for_this_user", lambda: False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(cwd), "session_id": sid,
                                                             "prompt": "go on"})))
    capsys.readouterr()
    assert hooks.main(["prompt"]) == 0
    out = capsys.readouterr().out
    fx.assert_no_fixture_name(out)
    return out


def test_desk_ask_writes_a_checked_file_beside_the_dev_asks(world, capsys):
    home, pr = world
    path = ask(world, "--blocks")
    assert path.name == "%s-%s-1.md" % (datetime.now(timezone.utc).date(), pr.code)
    data = devask.read(path)
    assert data["project"] == pr.code and data["blocks"] is True and data["state"] == "open"
    assert "two DNS records for the lab" in path.read_text()
    assert "notices/desk/" + path.name in capsys.readouterr().out
    assert not (home.shared / "notices/dev").exists() or not list((home.shared / "notices/dev").glob("*.md"))
    assert ask(world).name.endswith("-2.md")


def test_desk_ask_refuses_a_name_or_a_secret_and_outside_a_project(world, monkeypatch, capsys, tmp_path):
    home, _ = world
    ask(world)
    before = sorted(p.name for p in (home.shared / "notices/desk").iterdir())
    for text in (fx.CUSTOMER_FORMS[0], "pass" + "word=" + "invented-value-123"):
        assert cli.main(["desk", "ask", text]) == 2
        out = capsys.readouterr()
        fx.assert_no_fixture_name(out.out + out.err)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["desk", "ask", "an EIP"]) == 2
    assert "registered project" in capsys.readouterr().err
    assert sorted(p.name for p in (home.shared / "notices/desk").iterdir()) == before


def test_desk_session_sees_a_new_ask_once_and_project_or_checkout_sessions_do_not(world, monkeypatch, capsys,
                                                                                tmp_path):
    _, pr = world
    path = ask(world, "--blocks")
    desk = tmp_path / "desk-folder"
    desk.mkdir()
    checkout = tmp_path / "checkout"
    for rel in (".git", "awb", "seal/work-claude"):
        (checkout / rel).mkdir(parents=True)
    (checkout / "awb/cli.py").write_text("")
    (checkout / "seal/work-claude/CLAUDE.md").write_text("")
    assert "desk request" not in prompt(monkeypatch, capsys, checkout)
    assert "desk request" not in prompt(monkeypatch, capsys, pr.path)
    out = json.loads(prompt(monkeypatch, capsys, desk))["hookSpecificOutput"]["additionalContext"]
    assert out == ("desk request from %s: two DNS records for the lab [blocks] (notices/desk/%s, awb desk close %s "
                   "--note TEXT)" % (pr.code, path.name, path.name[:-3]))
    assert prompt(monkeypatch, capsys, desk) == ""
    assert "desk request from" in prompt(monkeypatch, capsys, desk, sid="uxr9-second")


def test_desk_list_and_close_deliver_the_note_and_close_only_that_ask(world, monkeypatch, capsys):
    home, pr = world
    first, second = ask(world), ask(world, text="an EIP for the second project")
    capsys.readouterr()
    assert cli.main(["desk", "list"]) == 0
    out = capsys.readouterr().out
    assert first.name[:-3] in out and second.name[:-3] in out and pr.code in out
    assert cli.main(["desk", "close", first.name[:-3], "--note", "both records are in"]) == 0
    assert "desk ask closed" in capsys.readouterr().out
    assert devask.read(first)["state"] == "closed" and devask.read(second)["state"] == "open"
    notes = sorted((Path(pr.path) / "notes/from-dev").glob("*.md"))
    assert len(notes) == 1 and "both records are in" in notes[0].read_text()
    assert notes[0].read_text().startswith("# desk answer: two DNS records for the lab")
    assert "desk answer: two DNS records" in rulesync.notice(home, "uxr9-work", Path(pr.path))
    assert cli.main(["desk", "list"]) == 0
    out = capsys.readouterr().out
    assert first.name[:-3] not in out and second.name[:-3] in out
    assert cli.main(["desk", "close", first.name[:-3], "--note", "again"]) == 2
    assert len(list((Path(pr.path) / "notes/from-dev").glob("*.md"))) == 1


def test_desk_close_with_owner_word_stamps_the_note(world, capsys):
    _, pr = world
    path = ask(world)
    assert cli.main(["desk", "close", path.name[:-3], "--note", "go ahead with the records",
                     "--owner-word", 'yes, "do both" today']) == 0
    note = sorted((Path(pr.path) / "notes/from-dev").glob("*.md"))[0].read_text()
    last = note.rstrip("\n").splitlines()[-1]
    assert last.startswith("owner's word: \"yes, 'do both' today\", ") and last.endswith("Z")
    assert ownerword.stamped(note)


def test_desk_close_refuses_a_name_in_his_words_and_keeps_the_ask_open(world, capsys):
    _, pr = world
    path = ask(world)
    before = path.read_bytes()
    assert cli.main(["desk", "close", path.name[:-3], "--note", "done", "--owner-word", fx.CUSTOMER_FORMS[0]]) == 2
    out = capsys.readouterr()
    fx.assert_no_fixture_name(out.out + out.err)
    assert path.read_bytes() == before
    assert not list((Path(pr.path) / "notes/from-dev").glob("*.md"))
    assert cli.main(["desk", "close", "../../etc/passwd", "--note", "done"]) == 2


def test_projects_note_with_owner_word_ends_with_the_stamp(world, monkeypatch, capsys, tmp_path):
    _, pr = world
    note = tmp_path / "note.md"
    note.write_text("the fix is live\n")
    assert cli.main(["projects", "note", pr.code, str(note), "--title", "fix live", "--owner-word",
                     "делай, the desk relays it"]) == 0
    text = sorted((Path(pr.path) / "notes/from-dev").glob("*.md"))[0].read_text()
    assert text.startswith("# fix live\n\nthe fix is live\n\nowner's word: \"делай, the desk relays it\", ")
    assert ownerword.stamped(text)
    assert cli.main(["projects", "note", pr.code, str(note), "--title", "x", "--owner-word", " "]) == 2


def test_dev_answer_with_owner_word_closes_the_dev_ask_with_the_stamp(world, monkeypatch, capsys):
    _, pr = world
    assert cli.main(["dev", "ask", "a filter for the docs"]) == 0
    path = sorted((Path(config.paths().shared) / "notices/dev").glob("*.md"))[-1]
    monkeypatch.setattr(sys, "stdin", io.StringIO("filter ready\n"))
    assert cli.main(["projects", "note", pr.code, "-", "--answers", str(path), "--title", "ready",
                     "--owner-word", "yes, ship it"]) == 0
    assert devask.read(path)["state"] == "closed"
    text = sorted((Path(pr.path) / "notes/from-dev").glob("*.md"))[0].read_text()
    assert ownerword.stamped(text) and "yes, ship it" in text


def test_stamp_shape():
    at = datetime(2026, 11, 20, 9, 30, 45, tzinfo=timezone.utc)
    assert ownerword.stamp("  go\n on ", at) == "owner's word: \"go on\", 2026-11-20T09:30Z"
    for bad in ("", "x" * (ownerword.MAX_WORDS + 1), "a\x00b"):
        with pytest.raises(ownerword.WordError):
            ownerword.stamp(bad, at)
    assert not ownerword.stamped("owner's word: go on")


def test_rules_carry_desk_ask_owner_word_and_version():
    work = (ROOT / "seal/work-claude/CLAUDE.md").read_text()
    assert len(work.splitlines()) <= 60
    assert "`awb desk ask" in work and "owner's word:" in work and "`awb version live COMMIT`" in work
    dev = (ROOT / "CLAUDE.md").read_text()
    assert "owner's word:" in dev and "`awb version live COMMIT`" in dev
