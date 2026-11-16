"""Rules that change reach every project and every running session (awb/rulesync.py).

2026-10-06: a session started on 2026-09-30 looked for a file in the vault inbox, because its project's CLAUDE.md and
the rules it had loaded predated the bucket inboxes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from awb import cli, rulesync
from tests.test_hooks import run_hook
from tests.test_lifecycle import spawn

OLD_CLAUDE = """# tcp-q7m4

A sealed lab of the workbench. Goal, customer code and tags are in SCOPE.md.

In this project:

- Start with SCOPE.md, STATE.md and OPEN.md. Update STATE.md and OPEN.md before the session ends.
- Codes only (project tcp-q7m4). Never write a real name, not even in a note or a commit.
- Customer material comes in only through `awb intake` and moves from the outbox into input/.
- Never open an original or the vault.
- Proof goes to evidence/, results to deliverables/, review records to reviews/.
- RESOURCES.md lists what the project uses, with platform ids and codes.
- A note of this project: the lab image lives in the lab bucket.
"""
OLD_STATE = ("# State of tcp-q7m4\n\nStatus: Started.\nNext: The first step.\n\n"
             "Where the work stands. Update it at the end of every session. Codes only, never a name.\n\n## Now\n")


def old_project(tmp_path: Path) -> Path:
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    (root / "CLAUDE.md").write_text(OLD_CLAUDE, encoding="utf-8")
    (root / "STATE.md").write_text(OLD_STATE, encoding="utf-8")
    return root


def test_the_sync_replaces_the_old_template_lines_and_keeps_the_project_notes(tmp_path):
    root = old_project(tmp_path)
    inode = (root / "CLAUDE.md").stat().st_ino
    assert rulesync.sync_project(root) == ["CLAUDE.md", "STATE.md"]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "before the session ends" not in text and "after every step that changes the status" in text
    assert "run `awb inbox take <his words>`, never look in the vault" in text
    lines = text.splitlines()
    assert lines[lines.index("- Proof goes to evidence/, results to deliverables/, review records to reviews/.") + 1] \
        .startswith("- Files for him leave with `awb xchg put FILE`")
    assert "- A note of this project: the lab image lives in the lab bucket." in lines
    assert (root / "CLAUDE.md").stat().st_ino == inode                    # the same file: owner and mode stay
    assert "after every step that changes the status, in the same commit" in (root / "STATE.md").read_text()
    assert rulesync.sync_project(root) == []                               # a second run changes nothing


def test_a_new_project_is_current_from_the_start(home, register_path, capsys):
    pr = spawn(home, register_path)
    text = (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "awb inbox take <his words>" in text and "awb xchg put FILE" in text
    assert rulesync.sync_project(Path(pr.path)) == []
    assert cli.main(["projects", "sync"]) == 0
    assert "%s: already current" % pr.code in capsys.readouterr().out


def test_a_running_session_hears_once_that_its_rules_changed(home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = old_project(tmp_path)
    assert rulesync.notice(home, None, root) is None                       # no session id: nothing to track
    assert rulesync.notice(home, "older-session", root) == rulesync.NOTICE  # older than the check: told once
    assert rulesync.notice(home, "older-session", root) is None
    rulesync.remember(home, "new-session", root)
    assert rulesync.notice(home, "new-session", root) is None
    rulesync.sync_project(root)                                            # the project's CLAUDE.md changes
    assert rulesync.notice(home, "new-session", root) == rulesync.NOTICE
    assert rulesync.notice(home, "new-session", root) is None
    assert not list((home.shared / "sessions").glob("rules-..*"))


def test_the_hooks_remember_at_the_start_and_tell_on_a_clean_prompt(home, register_path, monkeypatch, capsys):
    pr = spawn(home, register_path)
    payload = {"cwd": pr.path, "session_id": "a-session"}
    run_hook("session-start", payload, monkeypatch, capsys)
    assert run_hook("prompt", dict(payload, prompt="size the clusters"), monkeypatch, capsys) == (0, "", "")
    with open(Path(pr.path) / "CLAUDE.md", "a", encoding="utf-8") as fh:
        fh.write("- one more rule of this project\n")
    code, out, _ = run_hook("prompt", dict(payload, prompt="size the clusters"), monkeypatch, capsys)
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["additionalContext"] == rulesync.NOTICE
    assert run_hook("prompt", dict(payload, prompt="size the clusters"), monkeypatch, capsys) == (0, "", "")
    assert os.path.exists(home.shared / "sessions" / "rules-a-session.json")


def test_the_sync_brings_the_import_line_into_a_project_of_the_intake(tmp_path):
    """T4: a project made before wipe mode names `awb intake` for the copies of the outbox; the sync names his
    `awb import` and the notice of new input."""
    root = old_project(tmp_path)
    rulesync.sync_project(root)
    before = (root / "CLAUDE.md").read_text(encoding="utf-8")
    (root / "CLAUDE.md").write_text(before.replace("`awb import` wait in the outbox (the prompt says \"new input\" when "
                                                   "they come)", "`awb intake` wait in the outbox"), encoding="utf-8")
    assert rulesync.sync_project(root) == ["CLAUDE.md"]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Copies of his own `awb import` wait in the outbox" in text and "`awb intake`" not in text


def test_the_rules_notice_and_the_input_notice_come_in_one_text(home, tmp_path, monkeypatch):
    from tests import fixtures as fx

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = old_project(tmp_path)
    (home.outbox / fx.CUSTOMER_CODE).mkdir(parents=True, exist_ok=True)
    (home.outbox / fx.CUSTOMER_CODE / "F-ABCD.md").write_text("x", encoding="utf-8")
    rulesync.post_input(home, fx.CUSTOMER_CODE, root.name, ["F-ABCD"])
    said = rulesync.notice(home, "a-session", root)
    assert said.startswith(rulesync.NOTICE) and said.endswith(rulesync.INPUT % (1, fx.CUSTOMER_CODE))
    assert rulesync.notice(home, "a-session", root) is None


# Security regressions use only this test's files and in-process entry points.
def raw_notice(home, **fields):
    folder = rulesync.notices_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "input-20261115T120000Z-1234abcd.json"
    data = dict(customer="CUST-Q7M4", project="tcp-q7m4", ids=["F-ABCD"])
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def waiting_copy(home):
    path = home.outbox / "CUST-Q7M4" / "F-ABCD.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("copy", encoding="utf-8")
    return path


def test_notice_customer_cannot_leak_names_secrets_or_escape_outbox(home, tmp_path, capsys):
    from tests import fixtures as fx

    root = old_project(tmp_path)
    for index, customer in enumerate((fx.CUSTOMER_FORMS[1], "pass" + "word", str(tmp_path / "outside"),
                                      "../outside")):
        target = home.outbox / customer / "F-ABCD.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("outside", encoding="utf-8")
        raw_notice(home, customer=customer)
        result = rulesync.input_notices(home, "private-%d" % index, root)
        assert result == []
    assert capsys.readouterr() == ("", "")


def test_notice_requires_valid_project_even_for_customer_match(home, tmp_path, monkeypatch):
    root = old_project(tmp_path)
    waiting_copy(home)
    monkeypatch.setattr(rulesync, "_project_customer", lambda *args: "CUST-Q7M4")
    raw_notice(home, project="pass" + "word")
    assert rulesync.input_notices(home, "s", root) == []


def test_notice_rejects_wrong_field_types_and_deduplicates_ids(home, tmp_path):
    root = old_project(tmp_path)
    waiting_copy(home)
    for index, fields in enumerate(({"ids": 3}, {"ids": {"F-ABCD": True}}, {"replaced": "false"},
                                    {"auto": "true"})):
        raw_notice(home, **fields)
        assert rulesync.input_notices(home, "bad-%d" % index, root) == []
    raw_notice(home, ids=["F-ABCD", "F-ABCD", None, 3, "pass" + "word"])
    assert rulesync.input_notices(home, "good", root) == [rulesync.INPUT % (1, "CUST-Q7M4")]


def test_post_input_validates_types_without_echoing_values(home):
    import pytest

    secret = "pass" + "word"
    for customer, project, ids in (([secret], "tcp-q7m4", []), ("CUST-Q7M4", {secret: 1}, []),
                                   ("CUST-Q7M4", "tcp-q7m4", 42)):
        with pytest.raises(ValueError) as error:
            rulesync.post_input(home, customer, project, ids)
        assert secret not in str(error.value)
    path = rulesync.post_input(home, "CUST-Q7M4", "tcp-q7m4", [None, 1, secret, "F-ABCD", "F-ABCD"])
    assert json.loads(path.read_text())["ids"] == ["F-ABCD"]
    assert secret not in path.read_text()


def test_post_input_refuses_unicode_digits_in_file_ids(home):
    path = rulesync.post_input(home, "CUST-Q7M4", "tcp-q7m4", ["mail-٢٠٢٦-11-15-1.md", "F-ABCD"])
    assert json.loads(path.read_text())["ids"] == ["F-ABCD"]


def test_mark_rejects_duplicate_keys_and_excessive_nesting(tmp_path):
    path = tmp_path / "mark.json"
    for content in ('{"inputs": [], "inputs": ["hidden"]}', '{"x":' + '[' * 1500 + '0' + ']' * 1500 + '}'):
        path.write_text(content)
        assert rulesync._read_mark(path) == {}


def test_mark_read_is_bounded_even_when_stat_size_is_stale(tmp_path, monkeypatch):
    path = tmp_path / "mark.json"
    path.write_text(json.dumps({"x": "x" * (1024 * 1024 + 1)}))
    original = os.fstat

    def stale(fd):
        info = list(original(fd))
        info[6] = 1
        return os.stat_result(info)

    monkeypatch.setattr(os, "fstat", stale)
    assert rulesync._read_mark(path) == {}


def test_mark_rejects_file_and_directory_links_and_hardlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / "mark.json"
    original.write_text('{"digest":"outside"}')
    link = tmp_path / "link.json"
    link.symlink_to(original)
    hard = tmp_path / "hard.json"
    os.link(original, hard)
    directory = tmp_path / "linked"
    directory.symlink_to(outside, target_is_directory=True)
    for path in (link, hard, directory / "mark.json"):
        assert rulesync._read_mark(path) == {}


def test_invalid_mark_inputs_are_discarded_when_remembering_and_notifying(home, tmp_path):
    root = old_project(tmp_path)
    waiting_copy(home)
    raw_notice(home)
    path = rulesync._mark(home, "s")
    path.parent.mkdir(exist_ok=True)
    for bad in (23, True, {"hidden": True}, "hidden"):
        path.write_text(json.dumps({"inputs": bad, "notes": ["note"], "kb": {"last": "checked"}}))
        rulesync.remember(home, "s", root)
        data = json.loads(path.read_text())
        assert data["inputs"] == [] and data["notes"] == ["note"] and data["kb"] == {"last": "checked"}
        path.write_text(json.dumps({"inputs": bad}))
        assert rulesync.input_notices(home, "s", root) == [rulesync.INPUT % (1, "CUST-Q7M4")]


def test_session_ids_cannot_collide_after_sanitizing_or_truncating(home):
    ids = ["a/b", "ab", "!", "unknown", "a" * 80, "a" * 80 + "b", "a" * 80 + "c"]
    paths = [rulesync._mark(home, sid) for sid in ids]
    assert len(set(paths)) == len(ids)
    assert all(path.parent == home.shared / "sessions" for path in paths)
    assert rulesync._mark(home, "a-session").name == "rules-a-session.json"


def test_mark_write_does_not_follow_predictable_temp_link(tmp_path):
    target = tmp_path / "outside"
    target.write_text("untouched")
    path = tmp_path / "rules-s.json"
    path.with_name("." + path.name + ".tmp").symlink_to(target)
    rulesync._write_mark(path, {"inputs": []})
    assert target.read_text() == "untouched"
    assert json.loads(path.read_text()) == {"inputs": []}
    assert path.stat().st_mode & 0o777 == 0o600


def test_mark_write_refuses_parent_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "sessions"
    linked.symlink_to(outside, target_is_directory=True)
    rulesync._write_mark(linked / "rules-s.json", {"inputs": []})
    assert list(outside.iterdir()) == []


def test_failed_mark_replace_is_silent_and_removes_temp_files(tmp_path, monkeypatch, capsys):
    path = tmp_path / "rules-s.json"
    path.write_text('{"digest":"before"}')

    def denied(*args, **kwargs):
        raise PermissionError("pass" + "word")

    monkeypatch.setattr(os, "replace", denied)
    rulesync._write_mark(path, {"digest": "after"})
    assert json.loads(path.read_text()) == {"digest": "before"}
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
    assert capsys.readouterr() == ("", "")


def test_notice_publication_is_atomic_and_storage_errors_are_redacted(home, monkeypatch):
    import pytest

    original = os.fdopen

    class FailedWriter:
        def __enter__(self):
            return self

        def write(self, data):
            assert not list(rulesync.notices_dir(home).glob("input-*.json"))
            raise OSError("pass" + "word")

        def __exit__(self, *args):
            self.fh.close()

    def fail(fd, *args, **kwargs):
        writer = FailedWriter()
        writer.fh = original(fd, *args, **kwargs)
        return writer

    monkeypatch.setattr(os, "fdopen", fail)
    with pytest.raises(ValueError) as error:
        rulesync.post_input(home, "CUST-Q7M4", "tcp-q7m4", ["F-ABCD"])
    assert "pass" + "word" not in str(error.value)
    assert list(rulesync.notices_dir(home).iterdir()) == []


def test_outbox_links_never_count_as_waiting_copies(home, tmp_path):
    root = old_project(tmp_path)
    path = waiting_copy(home)
    raw_notice(home)
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    path.unlink()
    path.symlink_to(outside)
    assert rulesync.input_notices(home, "file-link", root) == []
    path.unlink()
    os.link(outside, path)
    assert rulesync.input_notices(home, "hard-link", root) == []
    path.unlink()
    path.parent.rmdir()
    folder = tmp_path / "outside-folder"
    folder.mkdir()
    (folder / path.name).write_text("outside")
    path.parent.symlink_to(folder, target_is_directory=True)
    assert rulesync.input_notices(home, "folder-link", root) == []


def test_sync_refuses_linked_roots_ancestors_and_hardlinked_files(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = old_project(outside)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    direct = tmp_path / "direct"
    direct.symlink_to(root, target_is_directory=True)
    for path in (linked / root.name, direct):
        assert rulesync.sync_project(path) == []
        assert not rulesync.current(path)
    os.link(root / "CLAUDE.md", tmp_path / "claude-hardlink")
    os.link(root / "STATE.md", tmp_path / "state-hardlink")
    assert rulesync.sync_project(root) == []
    assert (root / "CLAUDE.md").read_text() == OLD_CLAUDE


def test_sync_file_swap_cannot_redirect_the_write(tmp_path, monkeypatch):
    root = old_project(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    original = rulesync._fix_lines
    target = root / "CLAUDE.md"

    def swap(text, *args, **kwargs):
        if text == OLD_CLAUDE:
            target.unlink()
            target.symlink_to(outside)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(rulesync, "_fix_lines", swap)
    rulesync.sync_project(root)
    assert outside.read_text() == "untouched"


def test_sync_denied_write_does_not_echo_error_or_block_other_files(tmp_path, monkeypatch, capsys):
    import builtins

    root = old_project(tmp_path)
    target = root / "CLAUDE.md"
    original_open, original_os_open = builtins.open, os.open

    def denied_open(file, mode="r", *args, **kwargs):
        if Path(file) == target and "+" in mode:
            raise PermissionError("pass" + "word")
        return original_open(file, mode, *args, **kwargs)

    def denied_os_open(file, flags, *args, **kwargs):
        if Path(file).name == target.name and flags & os.O_RDWR:
            raise PermissionError("pass" + "word")
        return original_os_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied_open)
    monkeypatch.setattr(os, "open", denied_os_open)
    assert rulesync.sync_project(root) == ["STATE.md"]
    assert target.read_text() == OLD_CLAUDE
    assert capsys.readouterr() == ("", "")


def test_sync_register_path_must_be_under_configured_projects_root(home, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from awb import projects

    outside = old_project(tmp_path)
    monkeypatch.setattr(projects, "load", lambda p: [SimpleNamespace(code=outside.name, path=str(outside),
                                                                    state="active")])
    assert rulesync.sync(home) == []
    assert (outside / "CLAUDE.md").read_text() == OLD_CLAUDE


def test_digest_distinguishes_missing_empty_and_unreadable_files(tmp_path, monkeypatch):
    path = tmp_path / "rule"
    monkeypatch.setattr(rulesync, "_rules_files", lambda root: [path])
    missing = rulesync.digest(None)
    path.write_bytes(b"")
    assert rulesync.digest(None) != missing
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("rule")
    path.symlink_to(outside)
    assert rulesync.digest(None) == missing


def test_current_client_cannot_be_a_link(tmp_path, monkeypatch):
    from awb import projects

    installed = tmp_path / "installed"
    installed.write_text("current")
    monkeypatch.setattr(projects, "RULES_FILE", str(installed))
    client = tmp_path / "client"
    client.symlink_to(installed)
    assert not rulesync.current(None, client)


def test_concurrent_syncs_read_after_locking_and_keep_mode(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    root = old_project(tmp_path)
    (root / "CLAUDE.md").chmod(0o640)
    entered, release, started = Event(), Event(), Event()
    original = rulesync._fix_lines
    calls = []

    def slow(text, *args, **kwargs):
        if text == OLD_CLAUDE:
            calls.append(text)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
        return original(text, *args, **kwargs)

    def second():
        started.set()
        return rulesync.sync_project(root)

    monkeypatch.setattr(rulesync, "_fix_lines", slow)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(rulesync.sync_project, root)
        assert entered.wait(5)
        other = pool.submit(second)
        assert started.wait(5)
        # Give the second run a chance to read the stale content without a lock.
        import time
        time.sleep(0.1)
        release.set()
        results = first.result() + other.result()
    assert results.count("CLAUDE.md") == 1 and results.count("STATE.md") == 1
    assert len(calls) == 1
    assert (root / "CLAUDE.md").stat().st_mode & 0o777 == 0o640


def test_concurrent_input_notices_are_told_once_and_preserve_mark_fields(home, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import time

    root = old_project(tmp_path)
    waiting_copy(home)
    raw_notice(home)
    path = rulesync._mark(home, "s")
    rulesync._write_mark(path, {"notes": ["kept"], "kb": {"last": "kept"}})
    original = rulesync._read_mark
    start = Barrier(2)

    def slow(path):
        data = original(path)
        time.sleep(0.05)
        return data

    def run():
        start.wait(5)
        return rulesync.input_notices(home, "s", root)

    monkeypatch.setattr(rulesync, "_read_mark", slow)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(map(len, results)) == [0, 1]
    mark = json.loads(path.read_text())
    assert mark["notes"] == ["kept"] and mark["kb"] == {"last": "kept"}


def test_missing_unreadable_invalid_utf8_and_nonregular_rules_fail_closed(tmp_path, monkeypatch):
    root = old_project(tmp_path)
    (root / "CLAUDE.md").write_bytes(b"\xff")
    (root / "STATE.md").unlink()
    assert rulesync.sync_project(root) == []
    assert not rulesync.current(root)
    (root / "CLAUDE.md").unlink()
    (root / "CLAUDE.md").mkdir()
    assert rulesync.sync_project(root) == []
    assert not rulesync.current(root)
    (root / "CLAUDE.md").rmdir()
    assert rulesync.current(root)  # Missing optional rules retain established behavior.
    (root / "CLAUDE.md").write_text(OLD_CLAUDE)
    original = os.open

    def denied(file, *args, **kwargs):
        if Path(file).name == "CLAUDE.md":
            raise PermissionError("pass" + "word")
        return original(file, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    assert rulesync.sync_project(root) == []
    assert not rulesync.current(root)


def test_special_files_are_rejected_before_any_blocking_read(tmp_path, monkeypatch):
    import io
    import stat

    path = tmp_path / "CLAUDE.md"
    os.mkfifo(path)
    original_open, original_fdopen = io.open, os.fdopen

    def refuse_blocking(file, *args, **kwargs):
        if not isinstance(file, int) and Path(file) == path:
            raise AssertionError("attempted a blocking FIFO read")
        return original_open(file, *args, **kwargs)

    def refuse_fifo(fd, *args, **kwargs):
        assert not stat.S_ISFIFO(os.fstat(fd).st_mode), "FIFO passed the regular file check"
        return original_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(io, "open", refuse_blocking)
    monkeypatch.setattr(os, "fdopen", refuse_fifo)
    monkeypatch.setattr(rulesync, "_rules_files", lambda root: [path])
    assert rulesync._read_mark(path) == {}
    assert rulesync.sync_project(tmp_path) == []
    assert not rulesync.current(tmp_path)
    assert isinstance(rulesync.digest(tmp_path), str)


def test_oversized_rules_are_never_rewritten_or_reported_current(tmp_path, monkeypatch):
    from awb import projects

    root = old_project(tmp_path)
    path = root / "CLAUDE.md"
    text = OLD_CLAUDE + "x" * (1024 * 1024 + 1)
    path.write_text(text)
    assert rulesync.sync_project(root) == ["STATE.md"]
    assert path.read_text() == text
    assert not rulesync.current(root)
    monkeypatch.setattr(projects, "RULES_FILE", str(path))
    original = os.fstat

    def stale(fd):
        info = list(original(fd))
        info[6] = 1
        return os.stat_result(info)

    monkeypatch.setattr(os, "fstat", stale)
    assert not rulesync.current(None, path)


def test_malformed_notes_in_session_mark_do_not_crash_combined_notice(home, tmp_path):
    root = old_project(tmp_path)
    raw_notice(home)
    path = rulesync._mark(home, "s")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"digest": rulesync.digest(root), "notes": 42}))
    assert rulesync.notice(home, "s", root) is None


def test_concurrent_rule_change_notices_are_told_once(home, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import time

    root = old_project(tmp_path)
    original = rulesync._read_mark
    start = Barrier(2)

    def slow(path):
        data = original(path)
        time.sleep(0.05)
        return data

    def run():
        start.wait(5)
        return rulesync._rules_changed(home, "s", root)

    monkeypatch.setattr(rulesync, "_read_mark", slow)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results.count(rulesync.NOTICE) == 1 and results.count(None) == 1


def test_concurrent_mark_publications_use_independent_temp_files(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "rules-s.json"
    original = os.replace
    ready = Barrier(2)

    def together(*args, **kwargs):
        ready.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(os, "replace", together)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(rulesync._write_mark, path, {"digest": value}) for value in ("first", "second")]
        for future in futures:
            future.result()
    assert json.loads(path.read_text())["digest"] in ("first", "second")
    assert list(tmp_path.iterdir()) == [path]


def test_non_utf8_rule_path_is_hashed_without_echo(tmp_path, monkeypatch, capsys):
    path = tmp_path / os.fsdecode(b"rule-\xff")
    path.write_text("rule")
    monkeypatch.setattr(rulesync, "_rules_files", lambda root: [path])
    first = rulesync.digest(None)
    path.write_text("changed")
    assert rulesync.digest(None) != first
    assert capsys.readouterr() == ("", "")
