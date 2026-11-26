"""UX5: the open work queue, with temporary storage and an injectable UTC clock."""
from __future__ import annotations

import csv
import datetime as dt
import os

import pytest

from awb import cli, config
from tests import fixtures as fx


@pytest.fixture
def queue(home, tmp_path, monkeypatch):
    from awb import workqueue

    path = tmp_path / "work-profile" / "queue.tsv"
    monkeypatch.setenv("AWB_WORK_QUEUE", str(path))
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    clock = [dt.datetime(2026, 11, 11, 12, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(workqueue, "now", lambda: clock[0])
    return path, clock


def rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def add(who="desk", what="check the export", step="review the result"):
    return cli.main(["queue", "add", what, "--next", step, "--who", who])


def test_add_next_list_and_mine_keep_the_fields_and_dates(queue, capsys):
    path, clock = queue
    assert cli.main(["queue", "add", "check the export", "--next", "review the result",
                     "--who", "desk", "--project", "tcp-q7m4"]) == 0
    first = rows(path)[0]
    assert first["id"] in capsys.readouterr().out
    assert first["what"] == "check the export" and first["project"] == "tcp-q7m4"
    assert first["created"] == first["updated"] == first["next_changed"] == clock[0].isoformat()
    clock[0] += dt.timedelta(hours=1)
    assert cli.main(["queue", "next", first["id"], "send the export", "--who", "owner"]) == 0
    changed = rows(path)[0]
    assert changed["created"] == first["created"]
    assert changed["next"] == "send the export" and changed["who"] == "owner"
    assert changed["updated"] == changed["next_changed"] == clock[0].isoformat()
    assert add("session:UX5", "check the draft", "run the checks") == 0
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    out = capsys.readouterr().out
    assert all(word in out for word in ("tcp-q7m4", "send the export", "session:UX5", "run the checks"))
    assert cli.main(["queue", "list", "--mine"]) == 0
    out = capsys.readouterr().out
    assert "send the export" in out and "run the checks" not in out


def test_done_moves_the_row_to_the_archive_and_never_reuses_its_id(queue, capsys):
    path, clock = queue
    assert add() == 0
    first = rows(path)[0]
    clock[0] += dt.timedelta(hours=2)
    assert cli.main(["queue", "done", first["id"]]) == 0
    assert rows(path) == []
    archived = rows(path.with_name("queue-done.tsv"))[0]
    assert archived == dict(first, done=clock[0].isoformat())
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    assert first["id"] not in capsys.readouterr().out
    assert add() == 0
    assert rows(path)[0]["id"] != first["id"]


def test_owner_status_shows_only_owner_rows_with_their_next_step(queue, capsys):
    assert add("owner", step="approve the export") == 0
    assert add("development", step="run the checks") == 0
    capsys.readouterr()
    assert cli.main(["owner", "status"]) == 0
    out = capsys.readouterr().out
    assert "approve the export" in out and "check the export" in out
    assert "run the checks" not in out


def test_stale_uses_the_next_step_clock_and_same_step_does_not_reset_it(queue, capsys):
    path, clock = queue
    assert add() == 0
    ident = rows(path)[0]["id"]
    clock[0] += dt.timedelta(hours=23)
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    assert "stale" not in capsys.readouterr().out
    clock[0] += dt.timedelta(hours=2)
    assert cli.main(["queue", "next", ident, "review the result", "--who", "owner"]) == 0
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    assert "stale" in capsys.readouterr().out
    assert cli.main(["queue", "next", ident, "send the export", "--who", "owner"]) == 0
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    assert "stale" not in capsys.readouterr().out


def test_registered_names_are_refused_on_add_and_next_without_echo(queue, capsys):
    """DMZ 4: no content check inside, a registered name is kept as written on add and on next."""
    path, _ = queue
    assert add(what="call " + fx.PERSON_FORMS[0]) == 0
    assert rows(path)[0]["what"] == "call " + fx.PERSON_FORMS[0]
    assert cli.main(["queue", "next", rows(path)[0]["id"], "call " + fx.PERSON_FORMS[0],
                     "--who", "owner"]) == 0
    assert rows(path)[0]["next"] == "call " + fx.PERSON_FORMS[0]
    captured = capsys.readouterr()
    assert captured.err == ""


def test_reads_recheck_names_and_secrets_even_after_a_file_edit(queue, capsys):
    path, _ = queue
    assert add("owner") == 0
    capsys.readouterr()
    for planted in (fx.PERSON_FORMS[0], "password=" + "invented-value-8273"):
        original = path.read_text()
        path.write_text(original.replace("review the result", planted))
        assert cli.main(["queue", "list"]) == 0
        assert cli.main(["owner", "status"]) == 0
        output = capsys.readouterr()
        assert planted in output.out and output.err == ""
        path.write_text(original)


def test_missing_register_refuses_writes_and_listing(queue, home, capsys):
    path, _ = queue
    assert add() == 0
    home.register.unlink()
    # DMZ 4: the queue needs no register
    assert add() == 0
    assert cli.main(["queue", "list"]) == 0
    assert len(rows(path)) == 2


def test_owner_side_and_argument_shapes_are_enforced(queue, monkeypatch, capsys):
    path, _ = queue
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert add() == 2
    assert not path.exists()
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    for who in (fx.PERSON_FORMS[0], "session:", "session:../escape"):
        assert add(who) == 2
    assert cli.main(["queue", "add", "a task", "--next", "a step", "--who", "owner",
                     "--project", fx.CUSTOMER_FORMS[0]]) == 2
    assert add(step="first\nsecond") == 2
    assert cli.main(["queue", "done", "Q-9999"]) == 2
    output = capsys.readouterr()
    fx.assert_no_fixture_name(output.out + output.err)


def test_migration_keeps_only_open_rows_preserves_archive_and_runs_once(queue, capsys):
    path, clock = queue
    path.parent.mkdir()
    source = path.with_name("QUEUE.md")
    original = ("# Work queue\n\n| step | what | state | next |\n|---|---|---|---|\n"
                "| B1 | old export | done: abc123 | none |\n"
                "| B2 | another export | ACCEPTED | none |\n"
                "| B3 | check the export | waiting: his step | approve the export |\n"
                "| B4 | check the draft | open | run the checks |\n")
    source.write_text(original)
    old = (clock[0] - dt.timedelta(hours=25)).timestamp()
    os.utime(source, (old, old))
    assert cli.main(["queue", "migrate"]) == 0
    assert [r["id"] for r in rows(path)] == ["B3", "B4"]
    assert rows(path)[0]["who"] == "owner"
    assert source.read_text().splitlines()[0].startswith("Archive:")
    assert source.read_text().endswith(original)
    before = path.read_bytes(), source.read_bytes()
    assert cli.main(["queue", "migrate"]) == 0
    assert (path.read_bytes(), source.read_bytes()) == before
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    out = capsys.readouterr().out
    assert "B3" in out and "B4" in out and "stale" in out
    assert "B1" not in out and "B2" not in out
    assert len(rows(path)) < 40


def test_migration_refuses_names_without_marking_or_writing_the_queue(queue):
    path, _ = queue
    path.parent.mkdir()
    source = path.with_name("QUEUE.md")
    original = ("| id | what | state | next |\n|---|---|---|---|\n"
                "| X1 | call %s | waiting | review the result |\n" % fx.PERSON_FORMS[0])
    source.write_text(original)
    assert cli.main(["queue", "migrate"]) == 0
    assert source.read_text().startswith("Archive:") and source.read_text().endswith(original)
    assert rows(path)[0]["id"] == "X1" and fx.PERSON_FORMS[0] in rows(path)[0]["what"]


def test_failed_done_removal_keeps_archive_and_hides_completed_row(queue, monkeypatch, capsys):
    from awb import workqueue

    path, _ = queue
    assert add() == 0
    ident = rows(path)[0]["id"]
    replace = workqueue.os.replace

    def fail(src, dst):
        if dst == path:
            raise OSError("planted write failure")
        return replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(workqueue.os, "replace", fail)
        assert cli.main(["queue", "done", ident]) == 2
    assert rows(path.with_name("queue-done.tsv"))[0]["id"] == ident
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    assert ident not in capsys.readouterr().out
    assert cli.main(["queue", "done", ident]) == 0
    assert rows(path) == []
    assert len(rows(path.with_name("queue-done.tsv"))) == 1


def test_a_read_sends_only_new_or_edited_rows_to_the_name_check(queue, monkeypatch, capsys):
    """2026-11-14: a read checked every field of every row. DMZ 4: a read sends no row to the name check at all."""
    from awb import check, workqueue

    path, _ = queue
    assert add("owner") == 0 and add("owner") == 0
    capsys.readouterr()
    calls = []
    monkeypatch.setattr(check, "check_text", lambda *a, **k: calls.append(1) or [])
    workqueue.load(path)
    path.write_text(path.read_text().replace("review the result", "review the result again", 1))
    workqueue.load(path)
    assert cli.main(["queue", "list"]) == 0
    assert calls == []


def test_disk_cache_cannot_certify_a_secret(queue, capsys):
    """DMZ 4: no check, so no cache and no verdict file: a row with a secret shaped value is listed as written."""
    from awb import workqueue

    path, _ = queue
    assert add() == 0
    planted = "pass" + "word=" + "invented-value-8273"
    row = dict(rows(path)[0], next=planted)
    workqueue._write(path, [row])
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    output = capsys.readouterr()
    assert planted in output.out and output.err == ""
    assert not path.with_name("queue-checked.json").exists()
    assert not path.with_name("queue-verdicts.tsv").exists()


def test_corrupt_disk_cache_does_not_break_checked_reads(queue):
    from awb import workqueue

    path, _ = queue
    assert add() == 0
    for data in (b"\xff", b"[" * 20000 + b"]" * 20000):
        path.with_name("queue-checked.json").write_bytes(data)
        assert len(workqueue.load(path)) == 1


def test_cache_temporary_link_cannot_overwrite_an_outside_file(queue, tmp_path):
    from awb import workqueue

    path, _ = queue
    assert add() == 0
    outside = tmp_path / "outside"
    outside.write_text("keep this content")
    outside.chmod(0o640)
    path.with_name("queue-checked.json.tmp").symlink_to(outside)
    assert len(workqueue.load(path)) == 1
    assert outside.read_text() == "keep this content"
    assert outside.stat().st_mode & 0o777 == 0o640


def test_register_edit_with_same_size_and_mtime_invalidates_cache(queue, home, capsys):
    from awb import workqueue

    path, _ = queue
    with home.register.open("a") as stream:
        stream.write("PART-NEW2\tPART\treview the draft\t2026-11-11\tactive\n")
    assert add() == 0
    workqueue.load(path)
    before = home.register.stat()
    home.register.write_text(home.register.read_text().replace("review the draft", "check the export"))
    os.utime(home.register, ns=(before.st_atime_ns, before.st_mtime_ns))
    capsys.readouterr()
    # DMZ 4: a register edit changes nothing for the queue, it checks no row
    assert cli.main(["queue", "list"]) == 0
    assert capsys.readouterr().out


def test_unknown_register_revision_never_reuses_a_safety_verdict(queue, monkeypatch):
    """DMZ 4: no verdict and no stamp: a read of the queue calls no check, whatever the register says."""
    from awb import check, workqueue

    path, _ = queue
    assert add() == 0
    workqueue.load(path)

    def refused(*args, **kwargs):
        raise AssertionError("the queue runs no content check")

    monkeypatch.setattr(check, "check_text", refused)
    monkeypatch.setattr(check, "register_source", refused)
    assert len(workqueue.load(path)) == 1


def test_queue_storage_links_are_refused_including_dangling_links(queue, tmp_path, monkeypatch, capsys):
    from awb import workqueue

    _, clock = queue
    outside = tmp_path / "outside"
    outside.mkdir()
    for number, name in enumerate(("queue.tsv", "queue-done.tsv", "QUEUE.md", ".queue.lock")):
        for dangling in (False, True):
            folder = tmp_path / ("case-%d-%d" % (number, dangling))
            folder.mkdir()
            path = folder / "queue.tsv"
            target = outside / ("target-%d-%d" % (number, dangling))
            if not dangling:
                if name == "QUEUE.md":
                    target.write_text("| id | what | state |\n| X1 | check the export | open |\n")
                elif name == ".queue.lock":
                    target.write_text("keep")
                else:
                    row = workqueue._new("X1", "check the export", "review the result", "owner")
                    if name == "queue-done.tsv":
                        row["done"] = clock[0].isoformat()
                    workqueue._write(target, [row])
            before = target.read_bytes() if target.exists() else None
            (folder / name).symlink_to(target)
            monkeypatch.setenv("AWB_WORK_QUEUE", str(path))
            command = "migrate" if name == "QUEUE.md" else "list"
            assert cli.main(["queue", command]) == 2
            assert (target.read_bytes() if target.exists() else None) == before
            assert (folder / name).is_symlink()
    output = capsys.readouterr()
    assert not output.out and str(outside) not in output.err


def test_linked_parent_cannot_redirect_queue_writes(queue, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("AWB_WORK_QUEUE", str(linked / "nested" / "queue.tsv"))
    assert add() == 2
    assert list(outside.iterdir()) == []


def test_reserved_queue_filename_cannot_overwrite_sibling_storage(queue, monkeypatch):
    path, _ = queue
    for name in ("queue-done.tsv", ".queue.lock", "QUEUE.md", "queue-checked.json", "queue-checked.json.tmp"):
        monkeypatch.setenv("AWB_WORK_QUEUE", str(path.with_name(name)))
        assert add() == 2
        assert not path.with_name(name).exists()


def test_embedded_nul_in_storage_path_is_a_fixed_error(queue, monkeypatch, capsys):
    path, _ = queue
    planted = "pass" + "word=" + "invented-value-8273"
    monkeypatch.setenv("AWB_WORK_QUEUE", "unused")
    from awb import workqueue

    for bad in (path.with_name(planted + "\0"), path.parent / (planted + "\0") / "queue.tsv"):
        monkeypatch.setattr(workqueue, "queue_file", lambda: bad)
        assert add() == 2
        with pytest.raises(workqueue.QueueError):
            workqueue.load(bad)
    output = capsys.readouterr()
    assert not output.out and planted not in output.err


def test_parser_errors_never_echo_hostile_arguments(queue, capsys):
    planted = "pass" + "word=" + "invented-value-8273"
    for argv in (["''" + planted], ["list", "--bad='" + planted], ["add", "task", "--who"]):
        assert cli.main(["queue"] + argv) == 2
        output = capsys.readouterr()
        assert not output.out and planted not in output.err


def test_ledger_errors_never_echo_backend_values(queue, monkeypatch, capsys):
    from awb import ledger

    planted = "pass" + "word=" + "invented-value-8273"

    def unavailable(*args, **kwargs):
        raise ledger.LedgerError(planted + " " + fx.PERSON_FORMS[0])

    monkeypatch.setattr(ledger, "ensure_checkable", unavailable)
    # DMZ 4: the queue calls no ledger check, so a failing backend never reaches it
    assert add() == 0
    output = capsys.readouterr()
    assert planted not in output.out + output.err
    fx.assert_no_fixture_name(output.err)


def test_csv_parser_errors_are_safe_for_owner_status(queue, capsys):
    from awb import workqueue

    path, _ = queue
    path.parent.mkdir()
    path.write_text("\t".join(workqueue.FIELDS) + "\n" + "x" * (csv.field_size_limit() + 1))
    with pytest.raises(workqueue.QueueError, match="invalid queue"):
        workqueue.load(path)
    assert cli.main(["owner", "status"]) == 0
    assert "x" * 100 not in capsys.readouterr().out


def test_malformed_quoted_tsv_is_refused_without_rewriting(queue, capsys):
    path, _ = queue
    assert add() == 0
    # A closing quote followed by unquoted text was silently accepted by csv's default dialect.
    path.write_text(path.read_text().replace("review the result", '"review the" result'))
    before = path.read_bytes()
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 2
    assert path.read_bytes() == before and not capsys.readouterr().out


def test_unarchived_completed_rows_cannot_be_dropped_or_have_ids_reused(queue):
    from awb import workqueue

    path, clock = queue
    assert add() == 0
    workqueue._write(path, [dict(rows(path)[0], done=clock[0].isoformat())])
    source = path.with_name("QUEUE.md")
    source.write_text("| id | what | state |\n| X1 | check the export | open |\n")
    before = path.read_bytes()
    assert add() == 2
    assert cli.main(["queue", "migrate"]) == 2
    assert path.read_bytes() == before


def test_archive_rows_without_completion_dates_cannot_hide_open_work(queue):
    from awb import workqueue

    path, _ = queue
    assert add() == 0
    workqueue._write(path.with_name("queue-done.tsv"), rows(path))
    source = path.with_name("QUEUE.md")
    source.write_text("| id | what | state |\n| X1 | check the export | open |\n")
    before = path.read_bytes()
    assert add() == 2
    assert cli.main(["queue", "migrate"]) == 2
    assert path.read_bytes() == before


def test_duplicate_legacy_columns_do_not_silently_discard_work(queue):
    path, _ = queue
    path.parent.mkdir()
    source = path.with_name("QUEUE.md")
    original = "| id | what | what | state |\n| X1 | first task | second task | open |\n"
    source.write_text(original)
    assert cli.main(["queue", "migrate"]) == 2
    assert source.read_text() == original and not path.exists()


def test_missing_and_unreadable_storage_have_fixed_errors(queue, monkeypatch, capsys):
    from awb import workqueue

    path, _ = queue
    assert workqueue.load(path) == []
    assert cli.main(["queue", "migrate"]) == 2
    assert add() == 0
    before = path.read_bytes()
    real_read = workqueue._read
    planted = "pass" + "word=" + "invented-value-8273"

    def denied(file):
        if file == path:
            raise PermissionError(planted)
        return real_read(file)

    capsys.readouterr()
    with monkeypatch.context() as patch:
        patch.setattr(workqueue, "_read", denied)
        assert add() == 2
    assert path.read_bytes() == before
    output = capsys.readouterr()
    assert not output.out and planted not in output.err


def test_malformed_rows_are_refused_before_any_output(queue, capsys):
    from awb import workqueue

    path, _ = queue
    assert add() == 0
    valid = rows(path)[0]
    for field, value in (("created", "2026-11-11"), ("updated", "not-a-date"),
                         ("next_changed", "2026-11-11T12:00:00"), ("id", "../outside"),
                         ("next", "x" * 2001), ("what", "first\x1bsecond"), ("who", "session:../outside")):
        workqueue._write(path, [valid, dict(valid, id="X2", **{field: value})] if field != "id"
                         else [valid, dict(valid, id=value)])
        before = path.read_bytes()
        capsys.readouterr()
        assert cli.main(["queue", "list"]) == 2
        output = capsys.readouterr()
        assert not output.out and value not in output.err
        assert path.read_bytes() == before


def test_handles_and_outside_paths_are_screened_before_output(queue, capsys):
    from awb import workqueue

    path, _ = queue
    assert add() == 0
    valid = rows(path)[0]
    for field, value in (("id", fx.PERSON_FORMS[1]), ("who", "session:" + fx.PERSON_FORMS[1]),
                         ("next", "/home/" + "invented-owner/private")):
        workqueue._write(path, [dict(valid, **{field: value})])
        capsys.readouterr()
        # DMZ 4: a handle that reads like a name or a path is a handle: listed as written
        assert cli.main(["queue", "list"]) == 0
        output = capsys.readouterr()
        assert value in output.out and output.err == ""


def test_failed_archive_write_leaves_open_work_and_no_success_output(queue, monkeypatch, capsys):
    from awb import workqueue

    path, _ = queue
    assert add() == 0
    before = path.read_bytes()
    real_replace = workqueue.os.replace

    def fail(src, dst):
        if dst == path.with_name("queue-done.tsv"):
            raise OSError("planted failure")
        return real_replace(src, dst)

    monkeypatch.setattr(workqueue.os, "replace", fail)
    capsys.readouterr()
    assert cli.main(["queue", "done", rows(path)[0]["id"]]) == 2
    assert path.read_bytes() == before
    assert not path.with_name("queue-done.tsv").exists()
    assert not list(path.parent.glob(".queue-*"))
    assert not capsys.readouterr().out


def test_migration_retry_after_marker_failure_does_not_duplicate_rows(queue, monkeypatch):
    from awb import workqueue

    path, _ = queue
    path.parent.mkdir()
    source = path.with_name("QUEUE.md")
    original = "| id | what | state |\n| X1 | check the export | open |\n"
    source.write_text(original)
    real_replace = workqueue.os.replace

    def fail(src, dst):
        if dst == source:
            raise OSError("planted failure")
        return real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(workqueue.os, "replace", fail)
        assert cli.main(["queue", "migrate"]) == 2
    before = path.read_bytes()
    assert source.read_text() == original
    assert cli.main(["queue", "migrate"]) == 0
    assert path.read_bytes() == before and len(rows(path)) == 1


def test_concurrent_adds_keep_every_row_and_unique_ids(queue, monkeypatch):
    import multiprocessing
    import time
    from awb import workqueue

    path, _ = queue
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    real_open = workqueue._open

    def slow_open(path):
        result = real_open(path)
        time.sleep(0.1)
        return result

    monkeypatch.setattr(workqueue, "_open", slow_open)

    def run():
        assert start.wait(5)
        raise SystemExit(add())

    children = [ctx.Process(target=run) for _ in range(4)]
    try:
        for child in children:
            child.start()
        start.set()
        for child in children:
            child.join(10)
        assert [child.exitcode for child in children] == [0] * 4
        saved = rows(path)
        assert len(saved) == len({r["id"] for r in saved}) == 4
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(5)


def test_nonregular_storage_is_refused_without_blocking(queue):
    import multiprocessing

    path, _ = queue
    path.parent.mkdir()
    ctx = multiprocessing.get_context("fork")
    for name in ("queue.tsv", "queue-done.tsv", "QUEUE.md", ".queue.lock"):
        target = path.with_name(name)
        target.unlink(missing_ok=True)
        os.mkfifo(target)
        child = ctx.Process(target=lambda: os._exit(cli.main(["queue", "list"])))
        try:
            child.start()
            child.join(2)
            assert child.exitcode == 2
        finally:
            if child.is_alive():
                child.terminate()
            child.join(5)
            target.unlink()


def test_duplicate_ids_and_broken_headers_or_field_counts_refuse_the_whole_file(queue, capsys):
    path, _ = queue
    assert add() == 0
    header, row = path.read_text().splitlines(keepends=True)
    for broken in (header + row + row, header.replace("created", "id") + row,
                   header + row.rstrip("\n") + "\textra\n", header + "Q-0001\n"):
        path.write_text(broken)
        capsys.readouterr()
        assert cli.main(["queue", "list"]) == 2
        assert not capsys.readouterr().out
        assert path.read_text() == broken


def test_failed_archive_directory_sync_does_not_remove_open_work(queue, monkeypatch, capsys):
    import stat
    from awb import workqueue

    path, _ = queue
    assert add() == 0
    before = path.read_bytes()
    real_sync = workqueue.os.fsync

    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("planted directory sync failure")
        return real_sync(fd)

    monkeypatch.setattr(workqueue.os, "fsync", fail)
    capsys.readouterr()
    assert cli.main(["queue", "done", rows(path)[0]["id"]]) == 2
    assert path.read_bytes() == before
    assert len(rows(path.with_name("queue-done.tsv"))) == 1
    assert not capsys.readouterr().out


def test_a_link_swapped_in_after_validation_is_not_read(queue, tmp_path, monkeypatch, capsys):
    from awb import workqueue

    path, _ = queue
    assert add() == 0
    outside = tmp_path / "outside"
    outside.write_bytes(path.read_bytes())
    real_open = workqueue.os.open

    def swap(file, flags, *args, **kwargs):
        if file == path:
            path.unlink()
            path.symlink_to(outside)
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(workqueue.os, "open", swap)
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 2
    assert not capsys.readouterr().out


def test_hardlinked_storage_is_refused_without_changing_either_name(queue, tmp_path, capsys):
    path, _ = queue
    assert add() == 0
    outside = tmp_path / "outside"
    os.link(path, outside)
    before = outside.read_bytes()
    capsys.readouterr()
    assert add() == 2
    assert path.read_bytes() == outside.read_bytes() == before
    assert not capsys.readouterr().out
