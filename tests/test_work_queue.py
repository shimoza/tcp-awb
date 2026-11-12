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
    path, _ = queue
    assert add(what="call " + fx.PERSON_FORMS[0]) == 1
    assert not path.exists()
    assert add() == 0
    before = path.read_bytes()
    assert cli.main(["queue", "next", rows(path)[0]["id"], "call " + fx.PERSON_FORMS[0],
                     "--who", "owner"]) == 1
    assert path.read_bytes() == before
    captured = capsys.readouterr()
    fx.assert_no_fixture_name(captured.out + captured.err)


def test_reads_recheck_names_and_secrets_even_after_a_file_edit(queue, capsys):
    path, _ = queue
    assert add("owner") == 0
    capsys.readouterr()
    for planted in (fx.PERSON_FORMS[0], "password=" + "invented-value-8273"):
        original = path.read_text()
        path.write_text(original.replace("review the result", planted))
        assert cli.main(["queue", "list"]) == 1
        assert cli.main(["owner", "status"]) == 0
        output = capsys.readouterr()
        assert planted not in output.out + output.err
        path.write_text(original)


def test_missing_register_refuses_writes_and_listing(queue, home, capsys):
    path, _ = queue
    assert add() == 0
    before = path.read_bytes()
    home.register.unlink()
    assert add() == 2
    assert cli.main(["queue", "list"]) == 2
    assert path.read_bytes() == before


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
    assert cli.main(["queue", "migrate"]) == 1
    assert source.read_text() == original and not path.exists()


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
