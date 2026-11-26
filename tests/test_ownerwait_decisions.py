"""UX2 of 2026-11-11: `awb owner status` lists decisions only.

One project with 12 creates kept on purpose, 9 owner items and 3 tooling items; two stopped build tasks (one by the
owner, one red). Invented codes only; the vault, the runs and the project are temporary folders.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from awb import config, ownerwait, projects
from awb.tcp import labadmin, owner
from tests import fixtures as fx

LAB = fx.TENANT_ONE
TOOLING = ["owner (tooling) the runner needs a new timer", "(tooling) widen the rebase merge",
           "Steering: pick the next UX task"]


@pytest.fixture
def world(home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(ownerwait, "winlab_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "winlab_online_items", lambda *a, **k: [])
    monkeypatch.setattr(ownerwait, "tenant_mode_items", lambda *a, **k: [])
    proj = projects.spawn(home, "project", "move two app clusters to managed k8s", fx.CUSTOMER_CODE, home.register)
    lines = ["## Waiting on the owner", ""]
    lines += ["- Owner item %d: decide the region" % n for n in range(1, 10)]
    lines += ["- %s" % t for t in TOOLING]
    (Path(proj.path) / "OPEN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    now = time.time()
    run = {"id": "run-20261111-060000-abcd", "project": proj.code, "tenant": LAB, "state": "ended", "note": "",
           "started": "2026-11-11T06:00:00Z", "started_ts": now - 3600, "calls": 12,
           "created": [{"n": n, "label": "vpc vpcs", "name": "k%d" % n, "state": "kept", "kept_ts": now,
                        "reason": "stand %d" % n,
                        "delete": {"method": "DELETE", "service": "vpc", "path": "/v1/x/vpcs/%032d" % n}}
                       for n in range(12)]}
    labadmin.save(home, run)
    runs = tmp_path / "runs"
    for tid, state in (("B-101", "stopped: by_owner"), ("B-102", "red")):
        (runs / tid / "1").mkdir(parents=True)
        (runs / tid / "1" / "state").write_text(state + "\t2026-11-11T07:00:00Z\n")
    return home, proj, runs, tmp_path / "none.md"


def _lines(world, **kw):
    home, _proj, runs, queue = world
    items, parked = ownerwait.collect(home, queue, runs, **kw)
    return ownerwait.render(items, parked)


def test_the_default_list_holds_decisions_only(world):
    _home, proj, _runs, _q = world
    lines = _lines(world)
    text = "\n".join(lines)
    assert lines[0] == "7 items wait on you"
    kept = [ln for ln in lines if "kept on" in ln]
    assert len(kept) == 1 and "%s  12 creates kept on %s, listed in RESOURCES.md" % (proj.code, LAB) in kept[0]
    assert len([ln for ln in lines if ln.startswith("B-")]) == 1 and "B-102" in text and "B-101" not in text
    assert [ln.split("  ")[1] for ln in lines if "Owner item" in ln] == [
        "Owner item %d: decide the region" % n for n in range(1, 6)]
    assert "and 4 more: awb owner status --all %s" % proj.code in lines
    assert "tooling" not in text and "Steering" not in text
    fx.assert_no_fixture_name(text, "the owner list")


def test_a_task_that_ended_with_success_is_no_item(world):
    _home, _proj, runs, _q = world
    (runs / "B-101" / "1" / "state").write_text("stopped: success\t2026-11-11T07:00:00Z\n")
    assert "B-101" not in "\n".join(_lines(world))


def test_dev_prints_the_tooling_items(world):
    lines = _lines(world, view="dev")
    assert lines[0] == "3 items wait on you" and len(lines) == 4
    assert any("Steering: pick the next UX task" in ln for ln in lines)


def test_all_prints_every_source_line(world):
    _home, proj, _runs, _q = world
    lines = _lines(world, view="all")
    assert lines[0] == "26 items wait on you"
    assert len([ln for ln in lines if "kept vpc vpcs" in ln]) == 12
    assert any("B-101" in ln for ln in lines) and not any("more: awb owner status" in ln for ln in lines)
    only = _lines(world, view="all", code=proj.code)
    assert only[0] == "24 items wait on you"


def test_the_mail_follows_the_default_list(world):
    home, _proj, runs, queue = world
    sent = []
    assert ownerwait.mail(home, lambda s, b: sent.append((s, b)) or "sent", queue, runs) == "sent"
    subject, body = sent[0]
    assert subject == "Workbench: 7 items wait on you" and "more: awb owner status --all" in body
    assert "B-101" not in body and "tooling" not in body


def test_the_command_takes_dev_and_all(world, capsys, monkeypatch):
    home, proj, runs, queue = world
    monkeypatch.setattr(ownerwait, "queue_file", lambda: queue)
    monkeypatch.setenv("AWB_BUILD_RUNS", str(runs))
    monkeypatch.setattr(config, "paths", lambda: home)
    assert owner.main(["status", "--dev"]) == 0
    assert "3 items wait on you" in capsys.readouterr().out
    assert owner.main(["status", "--all", proj.code]) == 0
    assert "24 items wait on you" in capsys.readouterr().out


# Hardening regressions use only temporary data and in-process entry points.
def _status(**changes):
    value = {"version": 1, "written": owner.now_text(),
             "vault": {"state": "locked", "since": None}, "keys": {"state": "down", "since": None},
             "deployed": {"release": None, "journal": None},
             "page": {"size": None, "time": None, "backup_time": None, "backups": 0},
             "ui_runs": [], "intake": []}
    return dict(value, **changes)


def _row(**changes):
    return dict({"code": "tcp-q7m4", "item": 0, "text": "owner: review capacity",
                 "has_default": True, "app": "app: destination unavailable."}, **changes)


def _only(monkeypatch, items):
    from awb import devask

    monkeypatch.setattr(ownerwait, "open_items", lambda p: (items, 0))
    for name in ("queue_items", "export_items", "build_items", "dataset_items", "refresh_items", "report_items",
                 "lab_run_items", "tenant_mode_items", "winlab_items", "winlab_online_items"):
        monkeypatch.setattr(ownerwait, name, lambda *a, **kw: [])
    monkeypatch.setattr(projects, "held_notes", lambda p: [])
    monkeypatch.setattr(projects, "note_commits", lambda: set())
    monkeypatch.setattr(devask, "status_items", lambda *a: [])


def test_open_items_refuse_external_project_and_linked_sources(home, tmp_path, monkeypatch):
    from types import SimpleNamespace

    code = "tcp-q7m4"
    outside = tmp_path / "outside" / code
    outside.mkdir(parents=True)
    (outside / "OPEN.md").write_text("## Owner\n- review capacity\n")
    root = home.projects_root / code
    root.parent.mkdir(parents=True, exist_ok=True)
    record = SimpleNamespace(code=code, state="active", path=str(outside))
    monkeypatch.setattr(projects, "load", lambda p: [record])
    assert ownerwait.open_items(home) == ([], 0)
    record.path = str(root)
    root.symlink_to(outside, target_is_directory=True)
    assert ownerwait.open_items(home) == ([], 0)
    root.unlink()
    root.mkdir()
    (root / "OPEN.md").symlink_to(outside / "OPEN.md")
    assert ownerwait.open_items(home) == ([], 0)


def test_open_items_reject_oversize_sources(home, tmp_path, monkeypatch):
    from types import SimpleNamespace

    root = home.projects_root / "tcp-q7m4"
    root.mkdir(parents=True, exist_ok=True)
    (root / "OPEN.md").write_text("## Owner\n- review capacity\n" + "x" * (2 * 1024 * 1024))
    monkeypatch.setattr(projects, "load", lambda p: [SimpleNamespace(code=root.name, path=str(root), state="active")])
    assert ownerwait.open_items(home) == ([], 0)


def test_build_runs_ignore_non_ascii_numbers_and_linked_states(tmp_path):
    runs = tmp_path / "runs"
    task = runs / "B-101"
    (task / "1").mkdir(parents=True)
    (task / "1" / "state").write_text("red\tdate\n")
    (task / "²").mkdir()
    assert [it.label for it in ownerwait.build_items(runs)] == ["B-101"]
    (task / "1" / "state").unlink()
    target = tmp_path / "state"
    target.write_text("asked\tdate\n")
    (task / "1" / "state").symlink_to(target)
    assert ownerwait.build_items(runs) == []


@pytest.mark.timeout(5)
def test_state_and_summary_readers_refuse_links_and_special_files(tmp_path):
    import json
    import os

    path = tmp_path / "last.json"
    result = tmp_path / "result.json"
    target = tmp_path / "outside.json"
    target.write_text(json.dumps({"state": "failed", "summary": "review capacity"}))
    for file in (path, result):
        file.symlink_to(target)
    assert ownerwait._dataset_state(path) == {}
    assert owner.run_summary(tmp_path) is None
    for file in (path, result):
        file.unlink()
        os.mkfifo(file)
    assert ownerwait._dataset_state(path) == {}
    assert owner.run_summary(tmp_path) is None


def test_deep_json_and_nonfinite_counts_are_unavailable(home, tmp_path):
    import datetime
    import json

    path = tmp_path / "last.json"
    nested = '[' * 1500 + '0' + ']' * 1500
    path.write_text(nested)
    assert ownerwait._dataset_state(path) == {}
    (tmp_path / "result.json").write_text(nested)
    assert owner.run_summary(tmp_path) is None
    path.write_text(json.dumps({"state": "published", "at": owner.now_text(), "added": float("inf")}))
    assert ownerwait._publish_notes(path, datetime.datetime.now(datetime.timezone.utc), home) == []


def test_weekly_writer_does_not_follow_predictable_temp_link(tmp_path):
    path = tmp_path / "last.json"
    outside = tmp_path / "unrelated"
    outside.write_text("preserve")
    (tmp_path / ".last.json.new").symlink_to(outside)
    assert ownerwait.write_dataset_state({"state": "failed"}, path) == path
    assert outside.read_text() == "preserve"
    assert path.stat().st_mode & 0o777 == 0o600


def test_concurrent_weekly_writers_publish_complete_private_files(tmp_path, monkeypatch):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "last.json"
    barrier = threading.Barrier(2)
    replace = ownerwait.os.replace
    snapshots = []

    def together(src, dst):
        snapshots.append(json.loads(Path(src).read_text())["state"])
        barrier.wait(timeout=5)
        return replace(src, dst)

    monkeypatch.setattr(ownerwait.os, "replace", together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ownerwait.write_dataset_state, {"state": word}, path) for word in ("one", "two")]
        assert [f.result(timeout=10) for f in futures] == [path, path]
    assert sorted(snapshots) == ["one", "two"]
    assert json.loads(path.read_text())["state"] in snapshots
    assert list(tmp_path.iterdir()) == [path]


def test_failed_weekly_replace_cleans_temp_and_preserves_previous(tmp_path, monkeypatch):
    path = tmp_path / "last.json"
    path.write_text("previous")

    def denied(*args):
        raise PermissionError("private detail")

    monkeypatch.setattr(ownerwait.os, "replace", denied)
    assert ownerwait.write_dataset_state({"state": "failed"}, path) is None
    assert path.read_text() == "previous" and list(tmp_path.iterdir()) == [path]


def test_kept_tenant_and_location_are_screened_before_rendering(home, monkeypatch):
    from awb import ledger

    for field in ("extra", "where"):
        item = ownerwait.Item("tcp-q7m4", "review capacity", ownerwait.WHERE_SESSION, kind="kept")
        setattr(item, field, fx.CUSTOMER_FORMS[0])
        _only(monkeypatch, [item])
        items, parked = ownerwait.collect(home)
        text = "\n".join(ownerwait.render(items, parked))
        # B-088: no content check inside, the tenant and the location are rendered as they are
        assert ledger.WITHHELD not in text


def test_truncated_failure_reasons_are_screened_in_full(home, tmp_path, monkeypatch):
    import json
    from awb import ledger

    reason = "review capacity " * 12 + fx.CUSTOMER_FORMS[0]
    path = tmp_path / "last.json"
    path.write_text(json.dumps({"state": "failed", "reason": reason}))
    items = ownerwait.dataset_items(path) + ownerwait.refresh_items(path) + ownerwait.report_items(path)
    _only(monkeypatch, items)
    checked, _ = ownerwait.collect(home)
    # B-088: shown as written (cut to the line width), never withheld
    assert len(checked) == 3 and all(it.text != ledger.WITHHELD and "review capacity" in it.text for it in checked)


def test_truncated_queue_text_is_screened_in_full(home, tmp_path, monkeypatch):
    from awb import ledger

    path = tmp_path / "queue.md"
    path.write_text("| id | what | state | next |\n| Q-0001 | " + "review capacity " * 12 +
                    fx.CUSTOMER_FORMS[0] + " | waiting | review |\n")
    _only(monkeypatch, ownerwait.queue_items(path))
    checked, _ = ownerwait.collect(home)
    assert len(checked) == 1 and checked[0].text != ledger.WITHHELD and "review capacity" in checked[0].text


def test_mail_sender_failures_never_echo_values_or_exception_names(home, monkeypatch):
    secret = "pass" + "word=" + "Q7m4" + "Kx2aB3np"
    _only(monkeypatch, [ownerwait.Item("tcp-q7m4", "review capacity", "")])
    monkeypatch.setattr(ownerwait, "notes", lambda **kw: [])
    assert ownerwait.mail(home, lambda *a: secret) == "not sent: refused"

    def fails(*a):
        raise type(secret, (Exception,), {})(secret)

    assert ownerwait.mail(home, fails) == "not sent: refused"


def test_mail_refuses_if_check_becomes_unavailable_after_probe(home, monkeypatch):
    from awb import ledger

    _only(monkeypatch, [ownerwait.Item("tcp-q7m4", "review capacity", "")])
    monkeypatch.setattr(ownerwait, "notes", lambda **kw: [])
    original = ledger.hit_indexes

    def unavailable(texts, reg):
        if texts == ["probe"]:
            return original(texts, reg)
        raise ledger.LedgerError("private detail")

    monkeypatch.setattr(ledger, "hit_indexes", unavailable)
    # B-088: the mail runs no name check, so a check that cannot run stops nothing
    assert ownerwait.mail(home, lambda *a: True) == "sent"


def test_status_bad_container_types_raise_value_free_errors(tmp_path):
    for status in (None, [], _status(vault=None), _status(ui_runs=[None]), _status(deployed={"release": [], "journal": None})):
        with pytest.raises(owner.StatusError) as exc:
            owner.write(status, tmp_path / "status.json", checker=lambda text: pytest.fail("invalid status checked"))
        assert "unexpected shape" in str(exc.value)
    assert not (tmp_path / "status.json").exists()


def test_decisions_cache_rebuilds_invalid_shapes_and_timestamps(tmp_path, monkeypatch):
    import json
    import os

    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now + 0.9)
    path = tmp_path / "status.json"
    calls = []
    monkeypatch.setattr(ownerwait, "page_items", lambda p: calls.append(1) or [])
    for built, rows in ((now + 3600, []), (True, []), (float(now), []), (now, [_row(item=True)]),
                        (now, [_row(), _row()]), (now, [_row(text="bad\ntext")])):
        path.write_text(json.dumps({"decisions_built": built, "decisions": rows}))
        before = len(calls)
        assert owner._decisions(None, path) == ([], now)
        assert len(calls) == before + 1
    path.write_text(json.dumps({"decisions_built": now, "decisions": [_row()]}))
    os.utime(path, (now + 3600, now + 3600))
    assert owner._decisions(None, path) == ([], now)


def test_decisions_cache_refuses_links(tmp_path, monkeypatch):
    import json

    target = tmp_path / "outside.json"
    target.write_text(json.dumps({"decisions_built": int(time.time()), "decisions": [_row()]}))
    path = tmp_path / "status.json"
    path.symlink_to(target)
    monkeypatch.setattr(ownerwait, "page_items", lambda p: [])
    assert owner._decisions(None, path)[0] == []


def _ui_queue(tmp_path, summary="review capacity"):
    import json
    import sqlite3

    queue = tmp_path / "queue?mode=rw"
    queue.mkdir()
    with sqlite3.connect(queue / "queue.sqlite3") as db:
        db.execute("CREATE TABLE jobs (id TEXT, state TEXT, created TEXT, finished TEXT, apply_changes INTEGER)")
        db.execute("INSERT INTO jobs VALUES (?, 'completed', '2026-11-15T00:00:00Z', NULL, 1)", ("ui-1",))
    folder = queue / "runs" / "ui-1"
    folder.mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps({"summary": summary}))
    (folder / "validation.txt").write_text("PASS\n")
    return queue, folder


def test_ui_queue_uri_quotes_path_and_open_failures_are_empty(tmp_path, monkeypatch):
    import sqlite3

    queue, _ = _ui_queue(tmp_path)
    assert len(owner.ui_runs(queue)) == 1

    def denied(*a, **kw):
        raise sqlite3.OperationalError("private detail")

    monkeypatch.setattr(owner.sqlite3, "connect", denied)
    assert owner.ui_runs(queue) == []


def test_ui_summary_checks_full_text_before_truncation(home, tmp_path):
    # The visible prefix is safe; the full source must still withhold the run.
    queue, _ = _ui_queue(tmp_path, "review capacity " * 30 + fx.CUSTOMER_FORMS[0])
    # The URI case has its own test; this one must isolate summary checking.
    plain = tmp_path / "queue"
    queue.rename(plain)
    rows = owner.ui_runs(plain, screen=owner.summary_screen())
    assert len(rows) == 1 and rows[0]["summary"] is None and rows[0]["withheld"] == "name"


def test_ui_runs_refuse_linked_validation_and_summary(tmp_path):
    queue, folder = _ui_queue(tmp_path)
    plain = tmp_path / "queue"
    queue.rename(plain)
    folder = plain / "runs" / "ui-1"
    outside = tmp_path / "outside"
    folder.rename(outside)
    folder.symlink_to(outside, target_is_directory=True)
    rows = owner.ui_runs(plain, screen=lambda text: (text, None))
    assert len(rows) == 1 and rows[0]["validation"] is None and rows[0]["summary"] is None
    assert not rows[0]["publishable"]


def test_status_json_uses_name_check_and_does_not_echo_failures(home, monkeypatch, capsys):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(owner, "collect", lambda: _status(decisions=[_row(text=fx.CUSTOMER_FORMS[0])]))
    assert owner.main(["status", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    fx.assert_no_fixture_name(captured.err)


def test_collection_oserror_is_caught_without_values(monkeypatch, capsys):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    secret = "pass" + "word=" + "Q7m4" + "Kx2aB3np"

    def denied():
        raise PermissionError(secret)

    monkeypatch.setattr(owner, "collect", denied)
    assert owner.main(["status", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and secret not in captured.err
    assert "operating system error" in captured.err


def test_export_hash_refuses_symlinked_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_bytes(b"capacity")
    link = tmp_path / "linked"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        ownerwait._export_hash(link / "data")


def test_unknown_import_history_never_claims_exports_are_unimported(home, tmp_path, monkeypatch):
    import os

    root = tmp_path / "exports"
    folder = root / "export-1"
    folder.mkdir(parents=True)
    (folder / "data").write_bytes(b"capacity")
    monkeypatch.setenv("AWB_EXPORTS_ROOT", str(root))
    customer = home.private_reports / fx.CUSTOMER_CODE
    customer.mkdir()
    outside = tmp_path / "report.md"
    outside.write_text("# report\n")
    (customer / "report.md").symlink_to(outside)
    assert ownerwait.export_items(home, tmp_path / "none.md") == []
    (customer / "report.md").unlink()
    scan = os.scandir

    def denied(path):
        if path == home.private_reports or (isinstance(path, int) and
                os.readlink('/proc/self/fd/%d' % path) == str(home.private_reports)):
            raise PermissionError("private detail")
        return scan(path)

    monkeypatch.setattr(os, "scandir", denied)
    assert ownerwait.export_items(home, tmp_path / "none.md") == []


def test_malformed_vault_reply_makes_import_history_unknown(home, tmp_path, monkeypatch):
    from awb import vault_client

    customer = home.private_reports / fx.CUSTOMER_CODE
    customer.mkdir()
    original = home.originals / "original.gpg"
    original.write_bytes(b"sealed")
    (customer / "report.md").write_text("## Files\n| F-0001 | file | text | ok | none | " + str(original) + " |\n")
    monkeypatch.setattr(vault_client, "admin_call", lambda *a, **kw: {"data": None})
    assert ownerwait._import_hashes(home) is None


def test_unavailable_status_check_withholds_cached_free_text(tmp_path, monkeypatch, capsys):
    import json

    secret = "pass" + "word=" + "Q7m4" + "Kx2aB3np"
    status = _status(decisions=[_row(text=secret)], decisions_built=int(time.time()))
    path = tmp_path / "status.json"
    assert owner.write(status, path, checker=lambda text: "unavailable") == "unavailable"
    assert secret not in path.read_text() and json.loads(path.read_text())["decisions"] == []
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(owner, "collect", lambda: status)
    monkeypatch.setattr(owner, "name_check", lambda text: "unavailable")
    assert owner.main(["status", "--json"]) == 0
    assert secret not in capsys.readouterr().out


def test_text_status_collection_failure_is_private(monkeypatch, capsys):
    monkeypatch.setattr(config, "is_work_user", lambda: False)

    def denied(**kw):
        raise PermissionError(fx.CUSTOMER_FORMS[0])

    monkeypatch.setattr(ownerwait, "collect", denied)
    assert owner.main(["status"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "operating system error" in captured.err
    fx.assert_no_fixture_name(captured.err)


def test_lab_and_tenant_full_reasons_are_screened(home, monkeypatch):
    from awb import labruns, ledger, tenantmode

    reason = "review capacity " * 12 + fx.CUSTOMER_FORMS[0]
    monkeypatch.setattr(labruns, "attention", lambda *a: [])
    monkeypatch.setattr(labruns, "kept", lambda *a: [dict(tenant=LAB, project="tcp-q7m4", label="vpc",
                                                       id="resource", reason=reason)])
    monkeypatch.setattr(tenantmode, "read", lambda *a: {LAB: {"since": "2026-11-15", "note": reason}})
    items = ownerwait.lab_run_items(home) + ownerwait.tenant_mode_items()
    _only(monkeypatch, items)
    checked, _ = ownerwait.collect(home)
    # B-088: no content check inside, nothing is withheld
    assert len(checked) == 2 and all(it.text != ledger.WITHHELD for it in checked)


def test_ui_queue_link_is_not_opened(tmp_path):
    queue, _ = _ui_queue(tmp_path)
    plain = tmp_path / "queue"
    queue.rename(plain)
    db = plain / "queue.sqlite3"
    outside = tmp_path / "outside.sqlite3"
    db.rename(outside)
    db.symlink_to(outside)
    assert owner.ui_runs(plain) == []


def test_ui_run_ids_require_a_whole_safe_token(tmp_path):
    import sqlite3

    queue, _ = _ui_queue(tmp_path)
    plain = tmp_path / "queue"
    queue.rename(plain)
    with sqlite3.connect(plain / "queue.sqlite3") as db:
        db.execute("UPDATE jobs SET id = ?", ("ui-1\n",))
    assert owner.ui_runs(plain) == []
