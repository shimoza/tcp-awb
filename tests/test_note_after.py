"""UX12: a held development note waits for a successful release containing its commit."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awb import cli, deploy, ownerwait, projects, rulesync
from tests import fixtures as fx
from tests.test_deploy import commit, git, plan_host, synth_tree, world  # noqa: F401
from tests.test_lifecycle import run_hook, spawn


@pytest.fixture
def notes(home, register_path, tmp_path, monkeypatch):
    pr = spawn(home, register_path)
    for sid in ("ux12-session", "second-session"):
        rulesync.remember(home, sid, Path(pr.path))
    source = tmp_path / "note.md"
    source.write_text("Use the released command.\n", encoding="utf-8")
    journal = tmp_path / "DEPLOYED"
    monkeypatch.setattr(projects, "NOTE_JOURNAL", journal, raising=False)
    repo = tmp_path / "history"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "feature").write_text("before")
    before = commit(repo)
    (repo / "feature").write_text("ready")
    needed = commit(repo)
    (repo / "feature").write_text("later")
    later = commit(repo)
    git(repo, "checkout", "-q", "-b", "other", before)
    (repo / "feature").write_text("unrelated")
    other = commit(repo)
    return pr, source, journal, repo, before, needed, later, other


def hold(notes, capsys, after=None, title=None):
    pr, source, _, _, _, needed, *_ = notes
    args = ["projects", "note", pr.code, str(source), "--after", after or needed]
    if title:
        args += ["--title", title]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert pr.code in out and "held" in out and (after or needed) in out
    fx.assert_no_fixture_name(out)
    return out


def live(notes, target, **extra):
    _, _, journal, repo, *_ = notes
    deploy.write_journal(journal, dict(state="done", target=target,
                                     commits=git(repo, "rev-list", target).splitlines(), **extra))


def prompt(notes, monkeypatch, capsys, sid="ux12-session"):
    pr = notes[0]
    code, out, err = run_hook("prompt", {"cwd": pr.path, "session_id": sid, "prompt": "continue"},
                              monkeypatch, capsys)
    assert code == 0 and not err
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def test_hidden_before_live_then_shown_once(notes, monkeypatch, capsys):
    hold(notes, capsys)
    live(notes, notes[4])
    assert prompt(notes, monkeypatch, capsys) == ""
    live(notes, notes[6])
    out = prompt(notes, monkeypatch, capsys)
    assert notes[0].code in out and "notes/from-dev/" in out and "read it first" in out
    assert prompt(notes, monkeypatch, capsys) == ""
    assert prompt(notes, monkeypatch, capsys, "second-session") == out


def test_a_nonancestor_never_releases(notes, monkeypatch, capsys):
    hold(notes, capsys)
    live(notes, notes[7])
    assert prompt(notes, monkeypatch, capsys) == ""
    assert notes[5] not in git(notes[3], "rev-list", notes[7]).splitlines()
    assert any(notes[5] in it.text for it in ownerwait.collect(view="dev")[0])


def test_incomplete_or_unreadable_journal_keeps_the_note_held(notes, monkeypatch, capsys):
    hold(notes, capsys)
    assert prompt(notes, monkeypatch, capsys) == ""
    notes[2].write_text("{")
    assert prompt(notes, monkeypatch, capsys) == ""
    live(notes, notes[6], pending=["unit.service"])
    assert prompt(notes, monkeypatch, capsys) == ""
    live(notes, notes[6], notes_ready=False)
    assert prompt(notes, monkeypatch, capsys) == ""
    live(notes, notes[6])
    data = json.loads(notes[2].read_text())
    data["state"] = "running"
    deploy.write_journal(notes[2], data)
    assert prompt(notes, monkeypatch, capsys) == ""


def test_owner_dev_lists_held_code_handle_and_commit(notes, capsys):
    hold(notes, capsys, title=fx.CUSTOMER_FORMS[0])
    items, _ = ownerwait.collect(view="dev")
    lines = "\n".join(ownerwait.render(items, 0))
    assert notes[0].code in lines and notes[5] in lines and "held" in lines and ".md" in lines
    fx.assert_no_fixture_name(lines)
    assert not any("held note" in it.text for it in ownerwait.collect()[0])
    live(notes, notes[6])
    assert not any("held note" in it.text for it in ownerwait.collect(view="dev")[0])


def test_deploy_ends_with_one_line_per_released_note(notes, home, world, capsys):
    hold(notes, capsys, after=world.c0)
    hold(notes, capsys, after=world.c0)
    hold(notes, capsys, after="c" * 40)
    conf = world.etc / "awb" / "paths.conf"
    with conf.open("a") as f:
        f.write("shared = %s\n" % home.shared)
    world.runner.plan = world.plan(commits=[world.c1, world.c0])
    assert world.main([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len([line for line in out if "released note" in line]) == 2
    assert all(notes[0].code in line and ".md" in line for line in out[-2:])
    journal = deploy.read_journal(world.root / "DEPLOYED")
    assert journal["commits"] == [world.c1, world.c0]
    assert world.main([]) == 0
    assert "released note" not in capsys.readouterr().out


def test_plan_records_only_target_ancestors_and_validates_them(notes, world, tmp_path, capsys):
    hold(notes, capsys)
    repo = synth_tree(tmp_path).root
    git(repo, "init", "-q")
    first = commit(repo)
    (repo / "awb" / "vault.py").write_text("READY = True\n")
    target = commit(repo)
    git(repo, "checkout", "-q", "-b", "other", first)
    (repo / "awb" / "vault.py").write_text("OTHER = True\n")
    unrelated = commit(repo)
    plan = deploy.owner_plan(deploy.Options(to=target), repo, plan_host(world, repo))
    assert set(plan["commits"]) == {first, target} and unrelated not in plan["commits"]
    plan["commits"] = [fx.CUSTOMER_FORMS[0]]
    with pytest.raises(deploy.Refused, match="commits"):
        deploy.validate_plan(plan, set(deploy.templates(deploy.DirTree(repo))))


def test_invalid_commit_and_secret_are_refused_without_output_values(notes, capsys):
    hold(notes, capsys)
    pr, source, *_ = notes
    for invalid in (fx.CUSTOMER_FORMS[0], "HEAD", "a" * 39, ""):
        assert cli.main(["projects", "note", pr.code, str(source), "--after", invalid]) == 2
        out = capsys.readouterr()
        fx.assert_no_fixture_name(out.out + out.err)
        assert not invalid or invalid not in out.err
    source.write_text("pass" + "word=" + "ux12-secret-value")
    assert cli.main(["projects", "note", pr.code, str(source), "--after", notes[5]]) == 2
    out = capsys.readouterr()
    assert "ux12-secret-value" not in out.out + out.err
