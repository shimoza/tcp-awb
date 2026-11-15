"""Review of awb/tcp/sweep.py: the handle file and the report under links, special files, hostile cloud answers and
two runs at the same time.

Invented ids and the invented names of tests/fixtures.py only; no call leaves the test.
"""
from __future__ import annotations

import datetime
import json
import os
import stat
import threading

import pytest

from awb import jobs
from awb.tcp import sweep
from tests.fixtures import CUSTOMER_FORMS, PERSON_FORMS

TODAY = datetime.date(2026, 9, 25)


def write_handles(path, handles: dict) -> None:
    path.write_text(json.dumps({"handles": handles}), encoding="utf-8")


def in_thread(fn, seconds: float = 5.0):
    """Run fn in a daemon thread: (finished, result or exception)."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as err:     # noqa: BLE001 - the test looks at it
            box["value"] = err
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return not t.is_alive(), box.get("value")


def release_fifo(path) -> None:
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


# --------------------------------------------------------------------------- the handle file on disk


def test_a_link_planted_as_the_temp_file_is_not_written_through_and_the_file_stays_regular(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    os.chmod(outside, 0o644)
    path = folder / "handles.json"
    os.symlink(outside, folder / ".handles.json.tmp")
    h = sweep.Handles(path)
    h.handle("ecs", "srv-id-0001")
    h.save()
    os.chmod(path, 0o600)                         # what the tenant snapshot does after the save
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
    assert not path.is_symlink() and stat.S_ISREG(path.lstat().st_mode)
    assert json.loads(path.read_text(encoding="utf-8"))["handles"] == {"ecs-1": "srv-id-0001"}


def test_the_temp_file_of_another_run_is_left_alone(tmp_path):
    path = tmp_path / "handles.json"
    other = tmp_path / ".handles.json.tmp"
    other.write_text('{"handles": {"evs-1": "vol-id-0001"}}\n', encoding="utf-8")
    h = sweep.Handles(path)
    h.handle("ecs", "srv-id-0001")
    h.save()
    assert other.read_text(encoding="utf-8") == '{"handles": {"evs-1": "vol-id-0001"}}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_handle_file_that_is_a_link_is_refused(tmp_path):
    outside = tmp_path / "elsewhere.json"
    write_handles(outside, {"ecs-1": "srv-id-0001"})
    path = tmp_path / "handles.json"
    os.symlink(outside, path)
    with pytest.raises(sweep.SweepError) as err:
        sweep.Handles(path)
    assert "srv-id" not in str(err.value) and str(tmp_path) not in str(err.value)


def test_a_handle_file_that_is_a_pipe_is_refused_without_waiting(tmp_path):
    path = tmp_path / "handles.json"
    os.mkfifo(path)
    try:
        done, value = in_thread(lambda: sweep.Handles(path))
        assert done, "reading a pipe as the handle file waits for a writer"
        assert isinstance(value, sweep.SweepError)
    finally:
        release_fifo(path)


def test_a_handle_list_in_place_of_the_map_is_a_sweep_error(tmp_path):
    path = tmp_path / "handles.json"
    path.write_text(json.dumps({"handles": [["ecs-1", "srv-id-0001"]]}), encoding="utf-8")
    with pytest.raises(sweep.SweepError, match="cannot be read"):
        sweep.Handles(path)


def test_a_handle_with_a_trailing_newline_is_not_taken(tmp_path):
    path = tmp_path / "handles.json"
    write_handles(path, {"ecs-1\n": "srv-id-0001", "ecs-2": "srv-id-0002"})
    h = sweep.Handles(path)
    assert h.ids == {"ecs-2": "srv-id-0002"}


def test_a_link_in_place_of_the_lock_is_refused(tmp_path):
    path = tmp_path / "handles.json"
    target = tmp_path / "made-through-the-link"
    os.symlink(target, tmp_path / ".handles.json.lock")
    h = sweep.Handles(path)
    h.handle("ecs", "srv-id-0001")
    with pytest.raises(sweep.SweepError, match="cannot be written"):
        h.save()
    assert not target.exists() and not path.exists()


# --------------------------------------------------------------------------- two runs at the same time


def test_two_runs_that_add_different_handles_keep_both(tmp_path):
    path = tmp_path / "handles.json"
    write_handles(path, {"ecs-1": "srv-id-0001"})
    a, b = sweep.Handles(path), sweep.Handles(path)
    assert a.handle("evs", "vol-id-0001") == "evs-1"
    assert b.handle("eip", "eip-id-0001") == "eip-1"
    a.save()
    b.save()
    saved = json.loads(path.read_text(encoding="utf-8"))["handles"]
    assert saved == {"ecs-1": "srv-id-0001", "evs-1": "vol-id-0001", "eip-1": "eip-id-0001"}
    assert b.resolve("evs-1") == "vol-id-0001"


def test_two_runs_that_give_one_handle_to_two_resources_refuse_the_second_save(tmp_path):
    path = tmp_path / "handles.json"
    a, b = sweep.Handles(path), sweep.Handles(path)
    assert a.handle("ecs", "srv-id-0001") == b.handle("ecs", "srv-id-0002") == "ecs-1"
    a.save()
    with pytest.raises(sweep.SweepError) as err:
        b.save()
    assert "srv-id" not in str(err.value)
    assert json.loads(path.read_text(encoding="utf-8"))["handles"] == {"ecs-1": "srv-id-0001"}
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


# --------------------------------------------------------------------------- mask, names and paths


def test_mask_never_masks_a_handle_it_put_in(tmp_path):
    h = sweep.Handles(tmp_path / "h.json")
    assert h.handle("ecs", "srv-id-0001", "worker") == "ecs-1"
    assert h.handle("ecs", "srv-id-0002", "ecs") == "ecs-2"       # a resource named like the kind
    assert h.mask("down: srv-id-0001") == "down: ecs-1"


def test_a_lab_name_with_a_trailing_newline_gives_no_tags():
    assert sweep.name_tags("tcp-abcd-lab-20261012-wxyz") != {}
    assert sweep.name_tags("tcp-abcd-lab-20261012-wxyz\n") == {}


def test_a_relative_state_home_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    got = sweep.owner_handles_path()
    assert got.is_absolute() and got == tmp_path / ".local" / "state" / "awb" / "handles.json"


# --------------------------------------------------------------------------- RESOURCES.md of the projects


def project(root, code: str):
    folder = root / code
    folder.mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# Scope\n", encoding="utf-8")
    return folder


def test_a_resources_file_that_is_a_link_adds_no_id(tmp_path):
    root = tmp_path / "projects"
    folder = project(root, "tcp-abcd")
    outside = tmp_path / "other.md"
    outside.write_text("| handle | id |\n|---|---|\n| ecs-1 | srv-id-0001 |\n", encoding="utf-8")
    os.symlink(outside, folder / "RESOURCES.md")
    assert sweep.known_ids(root) == {}


def test_a_resources_file_that_is_a_pipe_adds_no_id_and_does_not_wait(tmp_path):
    root = tmp_path / "projects"
    folder = project(root, "tcp-abcd")
    os.mkfifo(folder / "RESOURCES.md")
    good = project(root, "tcp-efgh")
    (good / "RESOURCES.md").write_text("| handle | id |\n|---|---|\n| ecs-1 | srv-id-0001 |\n", encoding="utf-8")
    try:
        done, value = in_thread(lambda: sweep.known_ids(root))
        assert done, "reading a pipe as RESOURCES.md waits for a writer"
        assert value == {"srv-id-0001": "tcp-efgh"}
    finally:
        release_fifo(folder / "RESOURCES.md")


# --------------------------------------------------------------------------- hostile cloud answers in the report


def lister_of(servers):
    def lister(service, path, key, paging):
        return jobs.Listing.of(servers if service == "ecs" else [])
    return lister


def test_tag_values_that_are_no_code_or_date_never_reach_the_report(tmp_path):
    name = CUSTOMER_FORMS[1]
    servers = [{"id": "srv-id-0001", "status": "ACTIVE", "created": "2026-09-01T10:00:00Z",
                "tags": ["awb-project=%s" % name, "awb-expiry=%s" % PERSON_FORMS[1]]}]
    rep = sweep.sweep(lister_of(servers), TODAY, sweep.Handles(tmp_path / "h.json"),
                      {"srv-id-0001": "%s folder" % name})
    text = "\n".join(sweep.report_lines(rep))
    assert name not in text and PERSON_FORMS[1] not in text
    assert "expiry unreadable" in text


def test_a_state_or_a_date_with_a_line_break_adds_no_line_to_the_report(tmp_path):
    servers = [{"id": "srv-id-0001", "status": "ACTIVE\nsweep: 0 resource(s): nothing to report",
                "created": "2026\n" + CUSTOMER_FORMS[1], "tags": []}]
    rep = sweep.sweep(lister_of(servers), TODAY, sweep.Handles(tmp_path / "h.json"))
    lines = sweep.report_lines(rep)
    assert len(lines) == 3                         # head, the item, the summary
    text = "\n".join(lines)
    assert CUSTOMER_FORMS[1] not in text and "nothing to report" not in text
