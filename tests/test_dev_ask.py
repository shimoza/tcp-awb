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


def _metadata(path, **changes):
    from awb import devask

    original = path.read_text()
    data = devask.read(path)
    data.update(changes)
    path.write_text("<!-- %s -->\n%s" % (json.dumps(data), original.split("\n", 1)[1]))


def test_deep_metadata_is_refused_without_breaking_other_asks(world):
    from awb import devask

    home, _ = world
    bad, good = ask(world), ask(world)
    bad.write_text("<!-- " + "[" * 10000 + "0" + "]" * 10000 + " -->\n")
    with pytest.raises(devask.AskError):
        devask.read(bad)
    assert [p for p, _ in devask.open_asks(home)] == [good]


def test_duplicate_metadata_keys_are_refused(world):
    from awb import devask

    path = ask(world)
    path.write_text(path.read_text().replace('{"project":', '{"state": "closed", "project":', 1))
    with pytest.raises(devask.AskError):
        devask.read(path)


def test_invalid_metadata_types_and_unicode_are_withheld(world):
    from awb import devask

    home, _ = world
    path = ask(world)
    original = path.read_text()
    for changes in ({"project": []}, {"blocks": 1}, {"title": "bad\ud800"},
                    {"created": "2026-01-01"}, {"created": {}}, {"title": "bad\x1b"}):
        path.write_text(original)
        _metadata(path, **changes)
        with pytest.raises(devask.AskError):
            devask.read(path)
        assert devask.open_asks(home) == []


def test_growth_after_stat_is_still_bounded(world, monkeypatch):
    from awb import devask

    path = ask(world)
    path.write_bytes(b"x" * (devask.MAX_BYTES + 1))
    real_stat = os.fstat

    def stale_size(fd):
        values = list(real_stat(fd))
        values[6] = 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", stale_size)
    with pytest.raises(devask.AskError):
        devask._bytes(path)


def test_ask_file_links_special_missing_and_unreadable_are_refused(world, monkeypatch, tmp_path):
    from awb import devask

    home, _ = world
    path = ask(world)
    saved = tmp_path / "original"
    path.rename(saved)
    for kind in ("missing", "link", "directory", "fifo", "bytes", "large"):
        if kind == "link":
            path.symlink_to(saved)
        elif kind == "directory":
            path.mkdir()
        elif kind == "fifo":
            os.mkfifo(path)
        elif kind == "bytes":
            path.write_bytes(b"\xff")
        elif kind == "large":
            path.write_bytes(b"x" * (devask.MAX_BYTES + 1))
        with pytest.raises(devask.AskError):
            devask.read(path)
        assert devask.open_asks(home) == []
        if kind == "directory":
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
    saved.rename(path)
    real_open = os.open

    def denied(name, *args, **kwargs):
        if Path(name).name == path.name:
            raise PermissionError("unreadable fixture")
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(devask.AskError):
        devask.read(path)
    assert devask.open_asks(home) == []


def test_answer_header_without_body_does_not_crash(world):
    from awb import devask

    home, pr = world
    path = ask(world)
    path.write_text(path.read_text().splitlines()[0])
    note = devask.answer(home, pr.code, str(path), "ready", "use the filter")
    assert note.is_file()
    assert devask.read(path)["state"] == "closed"


def test_answer_checks_existing_body_and_decoded_metadata(world, monkeypatch):
    from awb import devask, devnote

    home, pr = world
    path = ask(world)
    original = path.read_text()
    secret = "pass" + "word=" + "invented-value-123"
    calls = []
    monkeypatch.setattr(devnote, "write", lambda *a: calls.append(a) or Path("2026-11-15-1.md"))
    for value in (secret, fx.PERSON_FORMS[0]):
        for field in ("body", "title", "extra"):
            path.write_text(original)
            if field == "body":
                path.write_text(original + value)
            else:
                _metadata(path, **{field: value})
                path.write_text(path.read_text().replace(value, "".join("\\u%04x" % ord(c) for c in value)))
            before = path.read_bytes()
            with pytest.raises(devask.AskError) as err:
                devask.answer(home, pr.code, str(path), "ready", "use the filter")
            assert value not in str(err.value)
            assert path.read_bytes() == before
    assert calls == []


def test_answer_uses_one_checked_snapshot(world, monkeypatch):
    from awb import devask, devnote

    home, pr = world
    path = ask(world)
    real_bytes = devask._bytes
    reads = []
    secret = "pass" + "word=" + "invented-value-123"

    def changing_read(*args, **kwargs):
        reads.append(1)
        return real_bytes(*args, **kwargs) if len(reads) == 1 else "header\n" + secret

    monkeypatch.setattr(devask, "_bytes", changing_read)
    monkeypatch.setattr(devnote, "write", lambda *a: Path("2026-11-15-1.md"))
    devask.answer(home, pr.code, str(path), "ready", "use the filter")
    assert len(reads) == 1
    assert secret not in path.read_text()


def test_answer_delivery_failure_preserves_request(world, monkeypatch):
    from awb import devask, devnote

    home, pr = world
    path = ask(world)
    before = path.read_bytes()

    def fail(*args):
        raise devnote.NoteError("delivery refused")

    monkeypatch.setattr(devnote, "write", fail)
    with pytest.raises(devnote.NoteError):
        devask.answer(home, pr.code, str(path), "ready", "use the filter")
    assert path.read_bytes() == before
    assert sorted(path.parent.iterdir()) == [path]


def test_concurrent_answers_deliver_once(world, monkeypatch):
    import fcntl
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from awb import devask, devnote

    home, pr = world
    path = ask(world)
    entered, release, second, competing = Event(), Event(), Event(), Event()
    calls = []
    real_flock = fcntl.flock

    def observed_lock(fd, operation):
        if entered.is_set():
            competing.set()
        return real_flock(fd, operation)

    def deliver(*args):
        calls.append(args)
        if len(calls) > 1:
            competing.set()
        entered.set()
        assert release.wait(5)
        return Path("2026-11-15-1.md")

    def answer_one(started=None):
        if started:
            started.set()
        try:
            return devask.answer(home, pr.code, str(path), "ready", "use the filter")
        except devask.AskError:
            return None

    monkeypatch.setattr(devnote, "write", deliver)
    monkeypatch.setattr(fcntl, "flock", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(answer_one)
        try:
            assert entered.wait(5)
            other = pool.submit(answer_one, second)
            assert second.wait(5)
            assert competing.wait(5)
            assert not other.done()
        finally:
            release.set()
        results = [first.result(timeout=5), other.result(timeout=5)]
    assert len(calls) == 1
    assert results.count(None) == 1
    assert devask.read(path)["state"] == "closed"


def test_concurrent_asks_keep_distinct_complete_files(world):
    from concurrent.futures import ThreadPoolExecutor
    from awb import devask

    home, _ = world
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda n: devask.write(home, "request number %d" % n), range(8)))
    assert len(set(paths)) == 8
    assert {devask.read(p)["title"] for p in paths} == {"request number %d" % n for n in range(8)}
    assert all(p.stat().st_mode & 0o777 == 0o640 for p in paths)


def test_failed_ask_write_leaves_no_partial_request(world, monkeypatch):
    import stat
    from awb import devask

    home, _ = world
    path = ask(world)
    before = path.read_bytes()

    real_chmod = os.fchmod

    def fail(fd, mode):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("disk full")
        return real_chmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", fail)
    with pytest.raises((devask.AskError, OSError)):
        devask.write(home, "another request")
    assert list(path.parent.iterdir()) == [path]
    assert path.read_bytes() == before


def test_request_is_published_only_when_complete(world, monkeypatch):
    from awb import devask

    home, _ = world
    target = home.shared / devask.FOLDER
    real_fdopen = os.fdopen
    observed = []

    def inspecting_fdopen(*args, **kwargs):
        observed.extend(target.glob("*.md"))
        return real_fdopen(*args, **kwargs)

    monkeypatch.setattr(os, "fdopen", inspecting_fdopen)
    path = devask.write(home, "another request")
    assert observed == []
    assert devask.read(path)["state"] == "open"


def test_answer_outside_notices_and_linked_parent_are_refused(world, tmp_path):
    from awb import devask

    home, pr = world
    path = ask(world)
    outside = tmp_path / path.name
    outside.write_bytes(path.read_bytes())
    alias = tmp_path / "alias"
    alias.symlink_to(path.parent, target_is_directory=True)
    for target in (outside, alias / path.name):
        with pytest.raises(devask.AskError):
            devask.answer(home, pr.code, str(target), "ready", "use the filter")
    assert devask.read(path)["state"] == "open"
    assert not list((Path(pr.path) / "notes/from-dev").glob("*.md"))


def test_linked_ancestor_withholds_requests(world, tmp_path):
    from dataclasses import replace
    from awb import devask

    home, _ = world
    ask(world)
    alias = tmp_path / "alias"
    alias.symlink_to(home.shared.parent, target_is_directory=True)
    linked = replace(home, shared=alias / home.shared.name)
    assert devask.open_asks(linked) == []


def test_directory_swap_cannot_redirect_ask_write(world, monkeypatch, tmp_path):
    from awb import devask

    home, _ = world
    ask(world)
    target = home.shared / devask.FOLDER
    moved = target.with_name("saved-dev")
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = os.open
    swapped = []

    def swapping_open(name, flags, *args, **kwargs):
        if flags & os.O_CREAT and not swapped:
            swapped.append(True)
            target.rename(moved)
            target.symlink_to(outside, target_is_directory=True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    try:
        devask.write(home, "another request")
    except (devask.AskError, OSError):
        pass
    assert swapped
    assert list(outside.iterdir()) == []


def test_receipt_directory_swap_cannot_write_outside(world, monkeypatch, tmp_path):
    import hashlib
    from awb import devask

    home, _ = world
    path = ask(world)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    checkout = _checkout(tmp_path)
    sid = "receipt-session"
    marks = path.parent / (".seen-" + hashlib.sha256(sid.encode()).hexdigest())
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = os.open
    swapped = []

    def swapping_open(name, flags, *args, **kwargs):
        if flags & os.O_CREAT and Path(name).name == path.name and not swapped:
            swapped.append(True)
            marks.rename(marks.with_name("saved-marks"))
            marks.symlink_to(outside, target_is_directory=True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    devask.notices(home, str(checkout), sid)
    assert swapped
    assert list(outside.iterdir()) == []


def test_notice_storage_failure_is_silent_and_retryable(world, monkeypatch, tmp_path):
    from awb import devask

    home, _ = world
    path = ask(world)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    checkout = _checkout(tmp_path)
    real_open = os.open

    def denied(name, flags, *args, **kwargs):
        if flags & os.O_CREAT and Path(name).name == path.name:
            raise PermissionError("receipt denied")
        return real_open(name, flags, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(os, "open", denied)
        assert devask.notices(home, str(checkout), "retry-session") == []
    assert len(devask.notices(home, str(checkout), "retry-session")) == 1
    assert devask.notices(home, str(checkout), "retry-session") == []


def test_parser_errors_never_echo_hostile_arguments(world, capsys):
    from awb import devask

    secret = "pass" + "word=" + "invented-value-123"
    for value in (secret, fx.CUSTOMER_FORMS[0]):
        for args in (["'safe' " + value], ["ask", "safe", "--" + value], ["ask", "safe", "--command"]):
            capsys.readouterr()
            with pytest.raises(SystemExit) as err:
                devask.main(args)
            assert err.value.code == 2
            output = capsys.readouterr()
            assert value not in output.out + output.err


def test_unicode_input_refusal_has_no_traceback_or_value(world, capsys):
    from awb import devask

    for args in (["ask", "bad\ud800"], ["ask", "safe", "--command", "bad\ud800"],
                 ["ask", "safe", "--error", "bad\ud800"]):
        assert devask.main(args) == 2
        output = capsys.readouterr()
        assert "bad" not in output.out + output.err


def test_write_refuses_unregistered_inactive_and_external_projects(world, monkeypatch, tmp_path):
    from dataclasses import replace
    from awb import devask

    home, pr = world
    for rows in ([], [replace(pr, state="closed")], [replace(pr, path=str(tmp_path))]):
        monkeypatch.setattr(projects, "load", lambda *args, rows=rows: rows)
        with pytest.raises(devask.AskError):
            devask.write(home, "another request")
    assert not list((home.shared / devask.FOLDER).glob("*.md"))


def test_checker_and_io_errors_do_not_echo_values(world, monkeypatch, capsys):
    from awb import devask, ledger

    home, _ = world
    secret = "pass" + "word=" + "invented-value-123"
    for failure in (ledger.LedgerError, PermissionError):
        def fail(*args, **kwargs):
            raise failure(secret + fx.CUSTOMER_FORMS[0])

        with monkeypatch.context() as m:
            if failure is ledger.LedgerError:
                m.setattr(ledger, "screen", fail)
            else:
                m.setattr(os, "open", fail)
            assert devask.main(["ask", "another request"]) == 2
        output = capsys.readouterr()
        assert secret not in output.out + output.err
        fx.assert_no_fixture_name(output.out + output.err)
    assert not list((home.shared / devask.FOLDER).glob("*.md"))


def test_empty_control_and_oversized_asks_leave_no_files(world):
    from awb import devask

    home, _ = world
    for text in (" \n\t", "bad\x00text", "x" * devask.MAX_BYTES):
        with pytest.raises(devask.AskError):
            devask.write(home, text)
    assert not list((home.shared / devask.FOLDER).glob("*.md"))


def test_answer_directory_swap_preserves_external_file(world, monkeypatch, tmp_path):
    from awb import devask, devnote

    home, pr = world
    path = ask(world)
    moved = path.parent.with_name("saved-dev")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / path.name
    victim.write_text("keep this file")

    def deliver(*args):
        path.parent.rename(moved)
        path.parent.symlink_to(outside, target_is_directory=True)
        return Path("2026-11-15-1.md")

    monkeypatch.setattr(devnote, "write", deliver)
    devask.answer(home, pr.code, str(path), "ready", "use the filter")
    assert victim.read_text() == "keep this file"
    assert devask.read(moved / path.name)["state"] == "closed"


def test_concurrent_notices_claim_each_request_once(world, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from awb import devask

    home, _ = world
    ask(world)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    checkout = _checkout(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: devask.notices(home, str(checkout), "shared-session"), range(4)))
    assert sum(map(len, results)) == 1


def test_invalid_session_unicode_is_silent(world, monkeypatch, tmp_path):
    from awb import devask

    home, _ = world
    ask(world)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    assert devask.notices(home, str(_checkout(tmp_path)), "session\ud800") == []


def test_answer_io_error_is_safe_and_keeps_original(world, monkeypatch, capsys):
    from awb import devask, devnote

    home, pr = world
    path = ask(world)
    before = path.read_bytes()
    secret = "pass" + "word=" + "invented-value-123"

    def fail(*args, **kwargs):
        raise PermissionError(secret + fx.PERSON_FORMS[0])

    monkeypatch.setattr(devnote, "write", lambda *a: Path("2026-11-15-1.md"))
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(devask.AskError) as err:
        devask.answer(home, pr.code, str(path), "ready", "use the filter")
    assert secret not in str(err.value)
    fx.assert_no_fixture_name(str(err.value))
    assert path.read_bytes() == before
    assert sorted(path.parent.iterdir()) == [path]


def test_existing_receipt_directory_is_private(world, monkeypatch, tmp_path):
    import hashlib
    from awb import devask

    home, _ = world
    path = ask(world)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    session = "pass" + "word=" + "invented-value-123"
    marks = path.parent / (".seen-" + hashlib.sha256(session.encode()).hexdigest())
    marks.mkdir(mode=0o755)
    assert len(devask.notices(home, str(_checkout(tmp_path)), session)) == 1
    assert marks.stat().st_mode & 0o777 == 0o700
    assert (marks / path.name).stat().st_mode & 0o777 == 0o600
    assert session not in str(marks)
