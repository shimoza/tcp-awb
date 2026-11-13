"""UX9: owner status finds recent exports by content hash without displaying their contents."""
from __future__ import annotations

import base64
import datetime
import os
import shlex
from pathlib import Path

import pytest

from awb import config, importcmd, ledger, ownerwait, projects, report, vault, vault_client, workqueue
from tests import fixtures as fx


@pytest.fixture
def world(home, tmp_path, monkeypatch):
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    monkeypatch.setenv("HOME", str(owner_home))
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(ownerwait, "open_items", lambda *a, **k: ([], 0))
    for name in ("build_items", "dataset_items", "refresh_items", "report_items", "lab_run_items",
                 "tenant_mode_items", "winlab_items", "winlab_online_items"):
        monkeypatch.setattr(ownerwait, name, lambda *a, **k: [])
    monkeypatch.setenv("AWB_WORK_QUEUE", str(owner_home / "queue.tsv"))
    return home, owner_home


def _export(world, name="export-1", contents=b"cluster capacity 4\n"):
    folder = world[1] / name
    folder.mkdir(exist_ok=True)
    file = folder / "notes.txt"
    file.write_bytes(contents)
    return folder, file


def _items(world):
    items, parked = ownerwait.collect(world[0])
    assert parked == 0
    return items


def _one(world):
    items = _items(world)
    assert len(items) == 1
    return items[0]


def _logged(world, data, number=1):
    """Write the existing import log format and its stored original, with an unrelated file name."""
    p, _ = world
    original = p.originals / fx.CUSTOMER_CODE / ("F-%04d.txt" % number)
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(data)
    record = report.FileRecord("F-%04d" % number, "renamed.txt", "text", "ok", original=original)
    run = report.Run("2026-11-11", "11:12:00", fx.CUSTOMER_CODE, False, files=[record])
    path = p.private_reports / fx.CUSTOMER_CODE / ("2026-11-11-%06d.md" % number)
    report.write_private(path, run)
    return path, original


def test_imported_copy_is_absent_and_real_import_removes_fresh_export(world, capsys):
    p, _ = world
    _export(world, "export-previous", b"previous capacity 2\n")
    _logged(world, b"previous capacity 2\n")
    folder, file = _export(world)
    item = _one(world)
    assert ownerwait.render([item], 0)[1] == "export export-1: 1 files, not imported  [his terminal]"
    project = projects.spawn(p, "project", "compare cluster capacity", fx.CUSTOMER_CODE, p.register)
    content = file.read_bytes()
    assert importcmd.run(p, project.code, [file], tty=True, trusted=True) == 0
    capsys.readouterr()
    assert list(p.private_reports.glob("*/*.md"))
    # Import moves the original. Restore an identical renamed copy to prove suppression is by hash.
    (folder / "copy.txt").write_bytes(content)
    assert _items(world) == []


def test_newest_file_controls_age_with_strict_thirty_day_boundary(world, monkeypatch):
    folder, old = _export(world)
    assert _one(world).text == "1 files, not imported"
    now = datetime.datetime(2026, 11, 13, tzinfo=datetime.timezone.utc)

    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(ownerwait.datetime, "datetime", Clock)
    cutoff = now.timestamp() - 30 * 86400
    os.utime(old, (cutoff - 1, cutoff - 1))
    assert _items(world) == []  # a recent directory mtime is irrelevant
    nested = folder / "nested"
    nested.mkdir()
    fresh = nested / "new.bin"
    fresh.write_bytes(b"\xff\x00\xfe")
    os.utime(fresh, (cutoff + 1, cutoff + 1))
    assert _one(world).text == "2 files, not imported"
    os.utime(fresh, (cutoff, cutoff))
    assert _items(world) == []


def test_folder_code_gives_trusted_import_command(world):
    folder, _ = _export(world, "export-hcs-q7m4-20261111")
    item = _one(world)
    assert item.where == "his terminal"
    assert shlex.split(item.block) == ["awb", "import", "hcs-q7m4", "--trusted", str(folder)]


def test_matching_queue_row_takes_priority_without_prefix_matches(world):
    folder, _ = _export(world, "export-tcp-q7m4")
    path = workqueue.queue_file()
    workqueue._write(path, [workqueue._new("Q-0001", "take " + folder.name + "-extra", "review", "desk",
                                         "hcs-kx2a")])
    assert "tcp-q7m4" in _one(world).block
    workqueue._write(path, [workqueue._new("Q-0002", "take export", "review " + str(folder), "desk",
                                         "hcs-b3np")])
    assert shlex.split(_one(world).block) == ["awb", "import", "hcs-b3np", "--trusted", str(folder)]


def test_unknown_or_ambiguous_code_gives_shell_quoted_start_command(world):
    folder, _ = _export(world, "export-batch '$(true)")
    expected = ["awb", "start", "project", "--from", str(folder), "--trusted", "--goal", "..."]
    assert shlex.split(_one(world).block) == expected
    workqueue._write(workqueue.queue_file(), [
        workqueue._new("Q-0001", "take " + folder.name, "review", "desk", "tcp-q7m4"),
        workqueue._new("Q-0002", "take " + folder.name, "review", "desk", "hcs-kx2a")])
    assert shlex.split(_one(world).block) == expected


def test_file_names_and_content_never_enter_status_or_mail(world):
    folder, file = _export(world, contents=(fx.PERSON_FORMS[0] + "\npass" + "word=do-not-print\n").encode())
    file.rename(folder / (fx.CUSTOMER_FORMS[0] + ".txt"))
    item = _one(world)
    assert item.text == "1 files, not imported"
    sent = []
    assert ownerwait.mail(world[0], lambda s, b: sent.append(s + b) or "sent") == "sent"
    assert len(sent) == 1 and "export export-1: 1 files, not imported" in sent[0]
    fx.assert_no_fixture_name(sent[0])
    assert "do-not-print" not in sent[0] and ".txt" not in sent[0]


def test_registered_folder_name_is_withheld_with_its_command(world):
    _export(world, "export-" + fx.CUSTOMER_FORMS[1])
    item = _one(world)
    assert item.label == "?" and item.text == ledger.WITHHELD and item.block is None
    fx.assert_no_fixture_name("\n".join(ownerwait.render([item], 0)))


def test_secret_and_control_characters_in_folder_are_withheld(world):
    folder, _ = _export(world, "export-batch " + "pass" + "word=Q7m4Kx2aB3np9z8v")
    item = _one(world)
    assert item.text == ledger.WITHHELD and item.block is None
    assert "Q7m4Kx2aB3np9z8v" not in "\n".join(ownerwait.render([item], 0))
    folder.rename(folder.with_name("export-batch\nextra line"))
    item = _one(world)
    assert item.label == "?" and item.text == ledger.WITHHELD and item.block is None


def test_unavailable_name_check_withholds_export_and_refuses_mail(world, monkeypatch):
    _export(world)
    assert _one(world).text == "1 files, not imported"
    def unavailable(*args):
        raise ledger.LedgerError("check unavailable")

    monkeypatch.setattr(ledger, "hit_indexes", unavailable)
    item = _one(world)
    assert item.text == ledger.WITHHELD and item.block is None
    assert ownerwait.mail(world[0], lambda *a: pytest.fail("unchecked mail")) == "not sent: the name check cannot run"


def test_any_logged_hash_excludes_folder_but_unlogged_original_does_not(world):
    folder, file = _export(world)
    assert _one(world).text == "1 files, not imported"
    path, original = _logged(world, file.read_bytes())
    log = path.read_text()
    path.unlink()
    assert _one(world).text == "1 files, not imported"
    (folder / "unimported.txt").write_bytes(b"new capacity 9\n")
    path.write_text(log)
    assert original.exists() and _items(world) == []


def test_empty_nonexport_links_and_special_files_are_not_exports(world):
    folder, file = _export(world)
    assert _one(world).text == "1 files, not imported"
    file.unlink()
    outside = world[1] / "outside"
    outside.mkdir()
    (outside / "fresh.txt").write_bytes(b"capacity 8\n")
    (folder / "link.txt").symlink_to(outside / "fresh.txt")
    (folder / "nested").symlink_to(outside, target_is_directory=True)
    (world[1] / "export-link").symlink_to(outside, target_is_directory=True)
    os.mkfifo(folder / "pipe")
    assert _items(world) == []


def test_sealed_import_history_uses_plaintext_hash_without_displaying_it(world, monkeypatch):
    _, file = _export(world)
    assert _one(world).text == "1 files, not imported"
    path, original = _logged(world, file.read_bytes())
    fresh, _ = _export(world, "export-new", b"new capacity 9\n")
    sealed_original = original.with_suffix(".txt.gpg")
    sealed_report = path.with_suffix(".md.gpg")
    data = {str(sealed_original): file.read_bytes(),
            str(sealed_report): path.read_bytes().replace(str(original).encode(), str(sealed_original).encode())}
    for plain, sealed in ((original, sealed_original), (path, sealed_report)):
        plain.unlink()
        sealed.write_bytes(b"sealed")

    def opened(op, *args, **kwargs):
        assert op == "open_file"
        return {"data": base64.b64encode(data[kwargs["path"]]).decode()}

    monkeypatch.setattr(vault, "admin_call", opened)
    monkeypatch.setattr(vault_client, "admin_call", opened)
    assert _one(world).label == "export " + fresh.name


def test_unreadable_import_history_does_not_claim_unimported(world):
    _, file = _export(world)
    assert _one(world).text == "1 files, not imported"
    _, original = _logged(world, file.read_bytes())
    original.unlink()
    assert _items(world) == []


def test_history_sealed_after_import_keeps_fresh_exports_visible(world, monkeypatch):
    _, file = _export(world)
    assert _one(world).text == "1 files, not imported"
    path, original = _logged(world, file.read_bytes())
    fresh, _ = _export(world, "export-new", b"new capacity 9\n")
    sealed_original = original.with_name(original.name + ".gpg")
    sealed_report = path.with_name(path.name + ".gpg")
    # Vault encryption preserves the report bytes, including its original plaintext path.
    data = {str(sealed_original): original.read_bytes(), str(sealed_report): path.read_bytes()}
    for plain, sealed in ((original, sealed_original), (path, sealed_report)):
        plain.unlink()
        sealed.write_bytes(b"sealed")

    def opened(op, *args, **kwargs):
        assert op == "open_file"
        return {"data": base64.b64encode(data[kwargs["path"]]).decode()}

    monkeypatch.setattr(vault, "admin_call", opened)
    monkeypatch.setattr(vault_client, "admin_call", opened)
    assert _one(world).label == "export " + fresh.name
