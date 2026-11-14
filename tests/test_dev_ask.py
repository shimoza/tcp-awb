"""UX4: checked requests to development, one prompt notice and an answer back."""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from awb import cli, config, hooks, ownerwait, projects, rulesync
from tests import fixtures as fx

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def world(home, monkeypatch, tmp_path):
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.chdir(pr.path)
    monkeypatch.setattr(ownerwait, "export_items", lambda *a: [])
    monkeypatch.setattr(ownerwait, "winlab_items", lambda: [])
    monkeypatch.setattr(ownerwait, "winlab_online_items", lambda: [])
    monkeypatch.setattr(ownerwait, "tenant_mode_items", lambda: [])
    monkeypatch.setattr(ownerwait, "queue_file", lambda: tmp_path / "none.tsv")
    return home, pr


def ask(world, *options):
    home, _ = world
    assert cli.main(["dev", "ask", "missing docs filter", *options]) == 0
    return sorted((home.shared / "notices/dev").glob("*.md"))[-1]


def owner_status(monkeypatch, capsys, *options):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    capsys.readouterr()
    assert cli.main(["owner", "status", *options]) == 0
    out = capsys.readouterr().out
    fx.assert_no_fixture_name(out)
    return out


def _checkout(base: Path) -> Path:
    """A stand-in of the owner's repository checkout: the runner's fresh export has no .git."""
    co = base / "ux4-checkout"
    for rel in (".git", "awb", "seal/work-claude"):
        (co / rel).mkdir(parents=True, exist_ok=True)
    (co / "awb" / "cli.py").write_text("")
    (co / "seal" / "work-claude" / "CLAUDE.md").write_text("")
    return co


def prompt(monkeypatch, capsys, cwd=None, sid="ux4-session"):
    import tempfile

    if cwd is None:
        cwd = _checkout(Path(tempfile.mkdtemp(prefix="ux4-")))
    monkeypatch.setattr(hooks, "for_this_user", lambda: False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(cwd), "session_id": sid,
                                                             "prompt": "go on"})))
    capsys.readouterr()
    assert hooks.main(["prompt"]) == 0
    out = capsys.readouterr().out
    fx.assert_no_fixture_name(out)
    return out


def test_work_ask_keeps_code_text_command_error_and_blocks(world, capsys):
    home, pr = world
    path = ask(world, "--blocks", "--command", "awb doc find", "--error", "unsupported filter")
    body = path.read_text()
    assert path.name == "%s-%s-1.md" % (datetime.now(timezone.utc).date(), pr.code)
    assert all(t in body for t in (pr.code, "missing docs filter", "awb doc find", "unsupported filter"))
    from awb import devask
    assert devask.open_asks(home)[0][1]["blocks"] is True
    assert os.stat(path).st_mode & 0o777 == 0o640
    assert path.name in capsys.readouterr().out
    second = ask(world)
    assert second != path and second.name.endswith("-2.md")


def test_owner_repository_prompt_shows_each_request_once_per_session(world, monkeypatch, capsys, tmp_path):
    _, pr = world
    path = ask(world)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert prompt(monkeypatch, capsys, tmp_path) == ""
    line = "request from %s: missing docs filter (notices/dev/%s)" % (pr.code, path.name)
    out = json.loads(prompt(monkeypatch, capsys))
    assert out["hookSpecificOutput"]["additionalContext"] == line
    assert prompt(monkeypatch, capsys) == ""
    assert line in prompt(monkeypatch, capsys, sid="ux4-second")


def test_registered_name_in_any_ask_field_refuses_without_writing(world, capsys):
    home, _ = world
    ask(world)
    folder = home.shared / "notices/dev"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    for args in ([fx.CUSTOMER_FORMS[0]], ["safe", "--command", fx.PERSON_FORMS[0]],
                 ["safe", "--error", fx.ORG_FORMS[0]]):
        assert cli.main(["dev", "ask", *args]) == 2
        out = capsys.readouterr()
        fx.assert_no_fixture_name(out.out + out.err)
        assert "name" in out.err
        assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_secret_in_any_ask_field_refuses_without_writing(world, capsys):
    home, _ = world
    ask(world)
    before = list((home.shared / "notices/dev").iterdir())
    secret = "pass" + "word=" + "invented-value-123"
    for args in ([secret], ["safe", "--command", secret], ["safe", "--error", secret]):
        assert cli.main(["dev", "ask", *args]) == 2
        out = capsys.readouterr()
        assert secret not in out.out + out.err and "secret" in out.err
        assert list((home.shared / "notices/dev").iterdir()) == before


def test_unavailable_name_check_refuses_without_writing(world, capsys):
    home, _ = world
    ask(world)
    before = list((home.shared / "notices/dev").iterdir())
    home.register.unlink()
    assert cli.main(["dev", "ask", "another filter"]) == 2
    assert "name check" in capsys.readouterr().err
    assert list((home.shared / "notices/dev").iterdir()) == before


def test_answer_note_closes_only_its_ask_and_reaches_project(world, monkeypatch, capsys):
    home, pr = world
    path = ask(world)
    other = ask(world)
    assert path.name in owner_status(monkeypatch, capsys, "--dev")
    monkeypatch.setattr(sys, "stdin", io.StringIO("use the new filter\n"))
    assert cli.main(["projects", "note", pr.code, "--answers", str(path), "--title", "filter ready", "-"]) == 0
    out = owner_status(monkeypatch, capsys, "--dev")
    assert path.name not in out and other.name in out
    assert '"state": "closed"' in path.read_text()
    assert "filter ready" in rulesync.notice(home, "ux4-work", Path(pr.path))
    assert rulesync.notice(home, "ux4-work", Path(pr.path)) is None


def test_overdue_unanswered_asks_give_one_default_line_per_project(world, monkeypatch, capsys):
    _, pr = world
    paths = [ask(world), ask(world)]
    assert "development owes" not in owner_status(monkeypatch, capsys)
    from awb import devask
    for path in paths:
        text = path.read_text()
        data = devask.read(path)
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        path.write_text(text.replace(data["created"], old))
    line = "development owes %s an answer" % pr.code
    assert owner_status(monkeypatch, capsys).splitlines().count(line) == 1
    for path in paths:
        monkeypatch.setattr(sys, "stdin", io.StringIO("filter ready\n"))
        assert cli.main(["projects", "note", pr.code, "--answers", str(path), "--title", "ready", "-"]) == 0
    assert line not in owner_status(monkeypatch, capsys)


def test_invalid_answer_target_and_unsafe_answer_leave_ask_open(world, monkeypatch, capsys):
    home, pr = world
    path = ask(world)
    before = path.read_bytes()
    for code, target, body in (("tcp-zzzz", path, "ready"), (pr.code, path.parent / "missing.md", "ready"),
                               (pr.code, path, fx.CUSTOMER_FORMS[0])):
        monkeypatch.setattr(sys, "stdin", io.StringIO(body))
        assert cli.main(["projects", "note", code, "--answers", str(target), "--title", "ready", "-"]) == 2
        out = capsys.readouterr()
        fx.assert_no_fixture_name(out.out + out.err)
        assert path.read_bytes() == before
        assert not list((Path(pr.path) / "notes/from-dev").glob("*.md"))
    assert path.name in owner_status(monkeypatch, capsys, "--dev")


def test_tampered_ask_is_withheld_by_hook_and_status(world, monkeypatch, capsys):
    _, _ = world
    path = ask(world)
    path.write_text(path.read_text().replace("missing docs filter", fx.CUSTOMER_FORMS[0]))
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert prompt(monkeypatch, capsys) == ""
    assert path.name not in owner_status(monkeypatch, capsys, "--dev")


def test_escaped_metadata_is_checked_before_display(world, monkeypatch, capsys):
    path = ask(world)
    original = path.read_text()
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    secret = "pass" + "word=" + "invented-value-123"
    for value in (fx.CUSTOMER_FORMS[0], secret):
        data = json.loads(original.splitlines()[0][5:-4])
        data["title"] = value
        metadata = json.dumps(data).replace(value, "".join("\\u%04x" % ord(c) for c in value))
        path.write_text("<!-- %s -->\n%s" % (metadata, original.split("\n", 1)[1]))
        assert prompt(monkeypatch, capsys, sid=value) == ""
        out = owner_status(monkeypatch, capsys, "--dev")
        assert path.name not in out and value not in out


def test_owner_notice_does_not_need_write_access_to_work_sessions(world, monkeypatch, capsys):
    home, pr = world
    path = ask(world)
    sessions = home.shared / "sessions"
    sessions.mkdir()
    make_dir = config.make_dir

    def owner_make_dir(target, *args, **kwargs):
        if target == sessions or sessions in target.parents:
            raise PermissionError("work session directory is not owner writable")
        return make_dir(target, *args, **kwargs)

    monkeypatch.setattr(config, "make_dir", owner_make_dir)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert "request from %s:" % pr.code in prompt(monkeypatch, capsys)
    assert prompt(monkeypatch, capsys) == ""
    assert path.is_file()


def test_work_rules_send_tooling_bugs_to_dev(world):
    ask(world)
    text = (ROOT / "seal/work-claude/CLAUDE.md").read_text()
    assert "a tooling bug: `awb dev ask`, one plain line to him, then his task." in text
