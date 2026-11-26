"""UX6: dated owner items, evidence, closure and the daily stop reminder."""
from datetime import date, timedelta
from pathlib import Path
import json
import subprocess

import pytest

from awb import cli, hooks, ledger
from tests import fixtures


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def project(home, monkeypatch):
    root = home.projects_root / "tcp-q7m4"
    root.mkdir(parents=True)
    (root / "SCOPE.md").write_text("# scope\n")
    git(root, "init", "-q")
    git(root, "config", "user.name", fixtures.PERSON_CODE)
    git(root, "config", "user.email", "fixture@example.org")
    (root / "OPEN.md").write_text("# open\n- owner: legacy. Default: keep ecs-0 stopped.\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    monkeypatch.chdir(root)
    return root


def items(root, count):
    day = (date.today() - timedelta(days=2)).isoformat()
    text = "# open\n" + "".join(
        "- %s owner: decide ecs-%d. Default: keep ecs-%d stopped.\n" % (day, n, n)
        for n in range(1, count + 1))
    (root / "OPEN.md").write_text(text)
    return text


def test_sweep_lists_25_ages_and_six_proven_defaults(project, capsys):
    items(project, 25)
    for n in range(1, 4):
        git(project, "commit", "--allow-empty", "-qm", "Default taken: keep ecs-%d stopped." % n)
    (project / "RESOURCES.md").write_text("".join(
        "| ecs-%d | deleted | Default taken: keep ecs-%d stopped. |\n" % (n, n)
        for n in range(4, 7)))
    assert cli.main(["open", "sweep"]) == 0
    rows = [r for r in capsys.readouterr().out.splitlines() if r.startswith("| ")][1:]
    assert len(rows) == 25
    for n, row in enumerate(rows, 1):
        assert row.startswith("| %d | 2d | decide ecs-%d. | keep ecs-%d stopped. |" % (n, n, n))
    assert sum("taken" in r for r in rows) == 6
    assert "commit " in rows[0] and "RESOURCES.md:3" in rows[5]


def test_close_three_ticks_and_writes_exactly_one_ledger_line(project, home, capsys):
    original = items(project, 25)
    assert cli.main(["open", "close", "1", "2", "3", "--default"]) == 0
    changed = (project / "OPEN.md").read_text()
    assert changed.count("- [x] ") == 3
    assert all(line.replace("- ", "- [x] ", 1) in changed for line in original.splitlines()[1:4])
    from awb import projects
    assert projects.open_items(project) == 22
    records = [json.loads(line) for p in home.ledger.glob("*.jsonl") for line in p.read_text().splitlines()]
    assert len(records) == 1 and records[0]["project"] == project.name
    assert "1, 2, 3" in records[0]["done"] and "default" in records[0]["done"]
    capsys.readouterr()
    assert cli.main(["open", "sweep"]) == 0
    assert len([r for r in capsys.readouterr().out.splitlines() if r.startswith("| ")]) == 23


def test_gate_requires_date_only_for_new_items_in_index_and_files(project, capsys):
    path = project / "OPEN.md"
    old = path.read_text()
    path.write_text(old + "- owner: decide ecs-1. Default: keep ecs-1 stopped.\n")
    git(project, "add", "OPEN.md")
    # A dated working copy must not conceal the undated staged version.
    path.write_text(old + "- 2026-11-11 owner: decide ecs-1. Default: keep ecs-1 stopped.\n")
    assert cli.main(["gate", "--staged", "--no-register"]) == 1
    assert "owner-date  OPEN.md:3" in capsys.readouterr().out
    git(project, "add", "OPEN.md")
    assert cli.main(["gate", "--staged", "--no-register"]) == 0
    path.write_text(old + "- owner: decide ecs-2. Default: keep ecs-2 stopped.\n")
    assert cli.main(["gate", "--no-register", "OPEN.md"]) == 1


def test_gate_does_not_grandfather_duplicate_or_invalid_date(project, capsys):
    path = project / "OPEN.md"
    old = path.read_text()
    path.write_text(old + old.splitlines()[1] + "\n- 2026-02-30 owner: decide ecs-2.\n")
    git(project, "add", "OPEN.md")
    assert cli.main(["gate", "--staged", "--no-register"]) == 1
    out = capsys.readouterr().out
    assert "owner-date  OPEN.md:3" in out and "owner-date  OPEN.md:4" in out


def test_stop_reminder_at_21_once_per_day_and_project(project, home, monkeypatch, capsys):
    from awb import opensweep
    monkeypatch.setattr(hooks, "draft_ledger", lambda *a: False)
    monkeypatch.setattr(hooks, "draft_gate", lambda *a: [])
    monkeypatch.setattr(hooks, "_quietly", lambda *a: None)
    from awb import harvest, projects, status
    monkeypatch.setattr(harvest, "due", lambda *a: None)
    monkeypatch.setattr(projects, "mail_owner_commands", lambda *a: None)
    monkeypatch.setattr(status, "reminder", lambda *a: None)
    monkeypatch.setattr(opensweep, "today", lambda: date(2026, 11, 13))
    data = {"cwd": str(project)}
    items(project, 20)
    assert hooks.hook_stop(data) == 0
    assert capsys.readouterr().err == ""
    items(project, 21)
    assert hooks.hook_stop(data) == 2
    line = capsys.readouterr().err
    assert len(line.splitlines()) == 1 and "21" in line and "awb open sweep" in line
    assert hooks.hook_stop(data) == 0
    assert capsys.readouterr().err == ""
    monkeypatch.setattr(opensweep, "today", lambda: date(2026, 11, 14))
    assert hooks.hook_stop(data) == 2
    other = home.projects_root / "tcp-kx2a"
    other.mkdir()
    (other / "SCOPE.md").write_text("# scope\n")
    items(other, 21)
    assert hooks.hook_stop({"cwd": str(other)}) == 2


def test_legacy_sections_checked_items_and_unproven_defaults(project, capsys):
    (project / "OPEN.md").write_text(
        "## owner\n- legacy. Default: this holds.\n- [x] settled. Default: this holds.\n"
        "## done\n- owner: settled too.\n## work\n- session task\n"
        "- 2026-11-11 owner: decide ecs-8. Default: keep ecs-8 stopped.\n")
    git(project, "commit", "--allow-empty", "-qm", "Discuss default: keep ecs-8 stopped.")
    (project / "RESOURCES.md").write_text("| ecs-8 | planned | keep ecs-8 stopped. |\n")
    assert cli.main(["open", "sweep"]) == 0
    out = capsys.readouterr().out
    assert "unknown" in out and "taken" not in out
    assert len([r for r in out.splitlines() if r.startswith("| ")]) == 3


def test_sweep_withholds_names_secrets_and_unsafe_evidence(project, capsys):
    from awb import opensweep
    items(project, 1)
    text = (project / "OPEN.md").read_text()
    secret = "gh" + "p_" + "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi"
    (project / "OPEN.md").write_text(text +
        "- owner: %s. Default: keep ecs-2 stopped.\n" % fixtures.PERSON_FORMS[0] +
        "- owner: decide ecs-3. Default: %s.\n" % secret)
    (project / "RESOURCES.md").write_text(
        "| ecs-1 | %s | Default taken: keep ecs-1 stopped. |\n" % fixtures.CUSTOMER_FORMS[0])
    assert cli.main(["open", "sweep"]) == 0
    out = capsys.readouterr().out
    # DMZ 4: no content check inside, the items and the receipt show as written
    assert fixtures.PERSON_FORMS[0] in out and secret in out
    assert "[withheld]" not in out and "RESOURCES.md:1" in out
    assert len(opensweep.parse((project / "OPEN.md").read_text())) == 3
    # a control character is still withheld, where a table line needs it
    (project / "OPEN.md").write_text(text + "- owner: bad\x07text. Default: keep ecs-2 stopped.\n")
    assert cli.main(["open", "sweep"]) == 0
    out = capsys.readouterr().out
    assert out.count("[withheld]") == 2 and "\x07" not in out


def test_sweep_refuses_when_the_name_check_is_unavailable(project, home, monkeypatch, capsys):
    items(project, 1)
    home.register.unlink()
    # DMZ 4: the sweep needs no register and no name check, it runs without them
    assert cli.main(["open", "sweep"]) == 0
    output = capsys.readouterr()
    assert "decide ecs-1." in output.out and output.err == ""
    from awb import check
    def unavailable(*args, **kwargs):
        raise check.CheckUnavailable("name check unavailable")
    monkeypatch.setattr(check, "register_source", unavailable)
    assert cli.main(["open", "sweep"]) == 0
    output = capsys.readouterr()
    assert "decide ecs-1." in output.out and output.err == ""


def test_close_refuses_bad_selection_missing_default_and_ledger_failure(project, monkeypatch):
    from awb import opensweep
    original = items(project, 3)
    for args in (["1", "4", "--default"], ["0", "--default"], ["1"]):
        assert cli.main(["open", "close", *args]) == 2
        assert (project / "OPEN.md").read_text() == original
    def refuse(*a, **k):
        raise ledger.Refused("fixture refusal")
    monkeypatch.setattr(ledger, "add", refuse)
    assert cli.main(["open", "close", "1", "--default"]) == 2
    assert (project / "OPEN.md").read_text() == original
    (project / "OPEN.md").write_text("- owner: no default\n")
    with pytest.raises(opensweep.OpenError):
        opensweep.close(project, [1])


def test_work_rules_require_date_closure_and_sweep():
    rules = (Path(__file__).resolve().parents[1] / "seal/work-claude/CLAUDE.md").read_text()
    assert "- YYYY-MM-DD owner: TEXT. Default: X." in rules
    assert "after a step that carries a default out, close its item" in rules
    assert "more than 20 owner items" in rules and "awb open sweep" in rules


def test_hostile_arguments_are_never_echoed(project, capsys):
    from awb import opensweep
    secret = "pass" + "word"
    for value in (secret + "'tail", secret + '\"tail', "'" + secret + "'tail", "--" + secret):
        for args in ([value], ["close", value, "--default"], ["sweep", value]):
            assert opensweep.main(args) == 2
            output = capsys.readouterr()
            assert output.out == "" and secret not in output.err


def test_dependency_errors_never_echo_values(project, monkeypatch, capsys):
    from awb import check, opensweep
    secret = "pass" + "word"
    def unavailable(*args, **kwargs):
        raise check.CheckUnavailable(secret)
    monkeypatch.setattr(check, "register_source", unavailable)
    assert opensweep.main(["sweep"]) == 0
    assert secret not in capsys.readouterr().err
    def refused(*args, **kwargs):
        raise ledger.Refused(secret)
    monkeypatch.setattr(ledger, "add", refused)
    assert opensweep.main(["close", "1", "--default"]) == 2
    assert secret not in capsys.readouterr().err


def test_open_links_cannot_read_or_change_outside(project, tmp_path, capsys):
    import os
    from awb import opensweep
    path = project / "OPEN.md"
    outside = tmp_path / "outside.md"
    original = items(project, 21)
    outside.write_text(original)
    for hard in (False, True):
        path.unlink()
        if hard:
            os.link(outside, path)
        else:
            path.symlink_to(outside)
        assert opensweep.main(["sweep"]) == 2
        assert capsys.readouterr().out == ""
        assert opensweep.main(["close", "1", "--default"]) == 2
        with pytest.raises((opensweep.OpenError, OSError)):
            opensweep.reminder(project)
        assert outside.read_text() == original


def test_resource_links_do_not_supply_outside_receipts(project, tmp_path, capsys):
    import os
    from awb import opensweep
    outside = tmp_path / "receipts.md"
    outside.write_text("| ecs-0 | Default taken: keep ecs-0 stopped. |\n")
    path = project / "RESOURCES.md"
    for hard in (False, True):
        if hard:
            os.link(outside, path)
        else:
            path.symlink_to(outside)
        assert opensweep.main(["sweep"]) == 2
        assert capsys.readouterr().out == ""
        path.unlink()


def test_linked_project_parent_is_refused(project, tmp_path):
    from awb import opensweep
    alias = tmp_path / "alias"
    alias.symlink_to(project.parent, target_is_directory=True)
    with pytest.raises((opensweep.OpenError, OSError)):
        opensweep.close(alias / project.name, [1])
    assert "[x]" not in (project / "OPEN.md").read_text()


def test_project_requires_regular_scope(project, tmp_path):
    from awb import opensweep
    scope = project / "SCOPE.md"
    scope.unlink()
    for linked in (False, True):
        if linked:
            outside = tmp_path / "scope.md"
            outside.write_text("# scope\n")
            scope.symlink_to(outside)
        for action in (lambda: opensweep.sweep(project), lambda: opensweep.close(project, [1]),
                       lambda: opensweep.reminder(project)):
            with pytest.raises((opensweep.OpenError, OSError)):
                action()
    assert "[x]" not in (project / "OPEN.md").read_text()


def test_special_files_are_refused_without_blocking(project):
    import os
    import sys
    source = str(Path(__file__).resolve().parents[1])
    for filename in ("OPEN.md", "RESOURCES.md"):
        path = project / filename
        if path.exists():
            path.unlink()
        os.mkfifo(path)
        # Import this export explicitly, without an installed package or CLI entry point.
        code = ("import sys; sys.path.insert(0, %r)\n" % source +
                "from pathlib import Path\nfrom awb import opensweep\n" +
                "try:\n    opensweep.sweep(Path(%r))\n" % str(project) +
                "except (opensweep.OpenError, OSError):\n    pass\n" +
                "else:\n    raise AssertionError('special file accepted')\n")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code],
                                capture_output=True, timeout=5)
        assert result.returncode == 0
        path.unlink()
        if filename == "OPEN.md":
            items(project, 1)


def test_missing_unreadable_and_invalid_data_are_private(project, monkeypatch, capsys):
    from awb import opensweep
    path = project / "OPEN.md"
    path.unlink()
    assert opensweep.main(["sweep"]) == 2
    assert opensweep.reminder(project) is None
    path.write_bytes(b"\xff")
    assert opensweep.main(["close", "1", "--default"]) == 2
    assert path.read_bytes() == b"\xff"
    original = items(project, 1)
    def denied(*args, **kwargs):
        raise PermissionError("pass" + "word")
    monkeypatch.setattr(opensweep, "evidence", denied)
    assert opensweep.main(["sweep"]) == 2
    output = capsys.readouterr()
    assert output.out == "" and str(project) not in output.err
    assert "pass" + "word" not in output.err
    assert path.read_text() == original


def test_git_environment_cannot_supply_foreign_receipts(project, tmp_path, monkeypatch):
    from awb import opensweep
    other = tmp_path / "foreign"
    other.mkdir()
    git(other, "init", "-q")
    git(other, "-c", "user.name=fixture", "-c", "user.email=fixture@example.org", "commit",
        "--allow-empty", "-qm", "Default taken: keep ecs-0 stopped.")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert "taken" not in opensweep.sweep(project)


def test_git_does_not_search_parent_repository(project):
    from awb import opensweep
    git(project, "commit", "--allow-empty", "-qm", "Default taken: keep ecs-0 stopped.")
    child = project / "tcp-kx2a"
    child.mkdir()
    (child / "SCOPE.md").write_text("# scope\n")
    (child / "OPEN.md").write_text((project / "OPEN.md").read_text())
    assert "taken" not in opensweep.sweep(child)


def test_fences_require_matching_character_length_and_empty_closer():
    from awb import opensweep
    line = "- owner: decide ecs-0. Default: keep ecs-0 stopped.\n"
    for false_end in ("~~~~", "```", "```` example"):
        text = "````markdown\n" + false_end + "\n" + line + "````\n" + line
        parsed = opensweep.parse(text)
        assert len(parsed) == 1 and parsed[0].line == 5


def test_close_ticks_compact_checkbox_once(project):
    from awb import opensweep
    (project / "OPEN.md").write_text("## owner\n- [ ]decide ecs-1. Default: keep ecs-1 stopped.\n")
    assert opensweep.close(project, [1]) == 1
    text = (project / "OPEN.md").read_text()
    assert "- [x] decide ecs-1." in text and "[ ]" not in text


def test_terminal_controls_are_withheld(project, capsys):
    from awb import opensweep
    (project / "OPEN.md").write_text("- owner: decide\x1b[2J ecs-0. Default: keep ecs-0 stopped.\n")
    assert opensweep.main(["sweep"]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output and "[withheld]" in output


def test_overlapping_close_refuses_changed_numbering(project, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from awb import opensweep
    items(project, 2)
    first_inside = threading.Event()
    second_waiting = threading.Event()
    real_flock = opensweep.fcntl.flock
    def flock(stream, operation):
        if threading.current_thread().name.endswith("_1"):
            second_waiting.set()
        return real_flock(stream, operation)
    def add(*args, **kwargs):
        first_inside.set()
        assert second_waiting.wait(5)
    monkeypatch.setattr(opensweep.fcntl, "flock", flock)
    monkeypatch.setattr(ledger, "add", add)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="close") as pool:
        first = pool.submit(opensweep.close, project, [1])
        assert first_inside.wait(5)
        second = pool.submit(opensweep.close, project, [1])
        assert first.result(timeout=5) == 1
        with pytest.raises(opensweep.OpenError):
            second.result(timeout=5)
    assert len(opensweep.parse((project / "OPEN.md").read_text())) == 1


def test_reminder_links_cannot_overwrite_outside(project, home, tmp_path):
    import os
    from awb import opensweep
    items(project, 21)
    folder = home.ledger / ".open-sweep"
    folder.mkdir()
    path = folder / (project.name + ".day")
    outside = tmp_path / "outside"
    outside.write_text("keep this\n")
    for hard in (False, True):
        if hard:
            os.link(outside, path)
        else:
            path.symlink_to(outside)
        with pytest.raises((opensweep.OpenError, OSError)):
            opensweep.reminder(project, home)
        assert outside.read_text() == "keep this\n"
        path.unlink()
    folder.rmdir()
    folder.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((opensweep.OpenError, OSError)):
        opensweep.reminder(project, home)
    assert not (tmp_path / path.name).exists()


def test_concurrent_reminders_claim_once(project, home):
    from concurrent.futures import ThreadPoolExecutor
    from awb import opensweep
    items(project, 21)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: opensweep.reminder(project, home), range(16)))
    assert sum(result is not None for result in results) == 1


def test_receipts_require_exact_complete_defaults(project):
    from awb import opensweep
    (project / "RESOURCES.md").write_text(
        "| ecs-0 | proposal Default taken: keep ecs-0 stopped. |\n"
        "| ecs-1 | Default taken: keep ecs-0 stopped. tomorrow |\n")
    git(project, "commit", "--allow-empty", "-qm", "proposed Default taken: keep ecs-0 stopped.")
    assert "taken" not in opensweep.sweep(project)
    (project / "RESOURCES.md").write_text("| ecs-0 | Default taken: KEEP   ecs-0 stopped |\n")
    assert "taken (RESOURCES.md:1 ecs-0)" in opensweep.sweep(project)


def test_sweep_waits_for_close_to_finish_writing(project, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from awb import opensweep
    items(project, 2)
    writing = threading.Event()
    release = threading.Event()
    reading = threading.Event()
    def add(*args, **kwargs):
        (project / "OPEN.md").write_text("# partial write\n")
        writing.set()
        assert release.wait(5)
    def sweep():
        reading.set()
        return opensweep.sweep(project)
    monkeypatch.setattr(ledger, "add", add)
    with ThreadPoolExecutor(max_workers=2) as pool:
        closing = pool.submit(opensweep.close, project, [1])
        assert writing.wait(5)
        listing = pool.submit(sweep)
        assert reading.wait(5)
        try:
            # A reader must wait for the exclusive writer even during a partial write.
            from concurrent.futures import TimeoutError
            with pytest.raises(TimeoutError):
                listing.result(timeout=0.2)
        finally:
            release.set()
        assert closing.result(timeout=5) == 1
        assert "1 open owner items" in listing.result(timeout=5)


def test_oversized_data_is_refused(project, home, monkeypatch):
    from awb import opensweep
    original = items(project, 21)
    monkeypatch.setattr(opensweep, "_MAX_BYTES", len(original.encode()) + 32)
    path = project / "OPEN.md"
    path.write_text(original + "z" * 64)
    with pytest.raises(opensweep.OpenError):
        opensweep.sweep(project)
    with pytest.raises(opensweep.OpenError):
        opensweep.close(project, [1])
    assert path.read_text() == original + "z" * 64
    path.write_text(original)
    (project / "RESOURCES.md").write_text("z" * (len(original) + 64))
    with pytest.raises(opensweep.OpenError):
        opensweep.sweep(project)
    marker = home.ledger / ".open-sweep" / (project.name + ".day")
    marker.parent.mkdir()
    marker.write_text("z" * (len(original) + 64))
    with pytest.raises(opensweep.OpenError):
        opensweep.reminder(project, home)


def test_linked_git_directory_cannot_supply_outside_receipts(project, tmp_path):
    from awb import opensweep
    git(project, "commit", "--allow-empty", "-qm", "Default taken: keep ecs-0 stopped.")
    outside = tmp_path / "foreign.git"
    (project / ".git").rename(outside)
    (project / ".git").symlink_to(outside, target_is_directory=True)
    assert "taken" not in opensweep.sweep(project)


def test_close_preserves_mode_and_deduplicates_selection(project, home):
    import stat
    from awb import opensweep
    items(project, 3)
    path = project / "OPEN.md"
    path.chmod(0o640)
    before = path.stat()
    assert opensweep.close(project, [3, 1, 3]) == 2
    after = path.stat()
    assert (after.st_ino, after.st_uid, after.st_gid) == (before.st_ino, before.st_uid, before.st_gid)
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert path.read_text().count("- [x]") == 2
    records = [json.loads(line) for p in home.ledger.glob("*.jsonl") for line in p.read_text().splitlines()]
    assert len(records) == 1 and "1, 3" in records[0]["done"]


def test_receipt_normalization_ignores_only_one_final_period(project):
    from awb import opensweep
    (project / "RESOURCES.md").write_text("| ecs-0 | Default taken: keep ecs-0 stopped... |\n")
    assert "taken" not in opensweep.sweep(project)
