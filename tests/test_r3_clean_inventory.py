"""R3 of the report design: `awb clean inventory` lists the files under the named roots of the laptop without their
content, keeps the real paths in the laptop vault and exports a coded copy that `--put` sends to a fake bucket.
Invented names only, temporary folders."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awb import clean, cleaninventory, cli, inventory, laptop
from tests import fixtures as fx
from tests.obs_fake import FakeOBS

PROJECT = "tcp-q7m4"
NOW = datetime(2026, 10, 29, 10, 15, tzinfo=timezone.utc)


@pytest.fixture
def lap(tmp_path, monkeypatch):
    monkeypatch.setenv(laptop.HOME_ENV, str(tmp_path / "laptop"))
    monkeypatch.setenv(laptop.WORK_ENV, str(tmp_path / "work"))
    for name in laptop.PUT_ENV + laptop.PUT_OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)
    laptop.init()
    it = iter([f + "\n" for f in fx.CUSTOMER_FORMS] + ["\n"])
    laptop.ask_forms(fx.CUSTOMER_CODE, read=lambda: next(it, ""), tty=True, say=lambda s: None)
    return tmp_path


def _file(path: Path, data: bytes = b"some bytes\n", mtime: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _run(roots, **kw):
    kw.setdefault("now", NOW)
    lines = []
    res = cleaninventory.run([Path(r) for r in roots], say=lines.append, **kw)
    return res, lines


def _by_name(res, ending):
    return [r for r in res.rows if r["real path"].endswith(ending)]


# --------------------------------------------------------------------------- the shared row format


def test_r3_the_row_format_is_pinned_in_one_module():
    assert inventory.COLUMNS == ("id", "path", "suffix", "size", "mtime", "sha256", "md5", "project", "group",
                                 "rank", "place")
    assert inventory.HEADER == "\t".join(inventory.COLUMNS)
    assert inventory.PREFIX == "reports/inventory/"
    assert inventory.file_name("laptop", "20261029-101500") == "laptop-20261029-101500.tsv"
    assert inventory.file_name("host", "20261029-101500", "kunden") == "host-20261029-101500-kunden.tsv"
    assert inventory.row_id("laptop", "20261029-101500", 7) == "L-20261029-101500-0007"
    with pytest.raises(inventory.InventoryError):
        inventory.file_name("desk", "20261029-101500")
    assert set(inventory.PLACES) == {"laptop", "host", "owner-bucket", "lab-bucket"}


@pytest.mark.parametrize("stem", ["angebot", "Angebot (2)", "angebot v2", "angebot_final", "Angebot 2026-10-01",
                                  "angebot-20261001", "angebot 01.10.2026 entwurf", "angebot kopie", "angebot-new"])
def test_r3_the_group_stem_drops_copies_versions_and_dates(stem):
    assert inventory.norm_stem(stem) == "angebot"


def test_r3_the_group_keeps_a_word_that_is_the_whole_stem():
    assert inventory.norm_stem("final") == "final"
    assert inventory.group_of(PROJECT, "a/b/plan-v3.docx", ".docx") == PROJECT + "/plan.docx"
    assert inventory.group_of(PROJECT, inventory.WITHHELD, ".docx") == inventory.WITHHELD


# --------------------------------------------------------------------------- names


def test_r3_a_registered_name_in_a_folder_name_never_reaches_the_inventory(lap, monkeypatch):
    root = lap / "win" / "d" / "kunden"
    folder = root / fx.CUSTOMER_FORMS[0] / ("notes " + fx.CUSTOMER_FORMS[1])
    _file(folder / ("offer %s v2.docx" % fx.CUSTOMER_FORMS[2]))
    _file(root / fx.PLANTED_CANDIDATE / "minutes.txt", b"other\n")
    with FakeOBS() as bkt:
        res, lines = _run([root], project=PROJECT, put=True, client=bkt.client())
        exported = res.export.read_text(encoding="utf-8")
        sent = b"".join(bkt.objects.values()).decode("utf-8")
    for text, where in ((exported, "the coded copy"), (sent, "the bucket"), ("\n".join(lines), "the output")):
        fx.assert_no_fixture_name(text, where)
        assert fx.PLANTED_CANDIDATE.split()[0] not in text, "a candidate company becomes a token"
    paths = [r["path"] for r in inventory.read(res.export)]
    assert any(p.startswith(fx.CUSTOMER_CODE + "/") for p in paths), "the registered folder becomes the code"
    assert any(p.startswith("company-1/") for p in paths), "an unregistered company becomes a token"
    # the check can fail: the full list in the vault does carry the real folder name
    assert fx.CUSTOMER_FORMS[0] in res.local.read_text(encoding="utf-8")


def test_r3_a_name_split_across_folders_withholds_the_whole_path(lap):
    root = lap / "root"
    short = fx.CUSTOMER_FORMS[1]
    data = b"split\n"
    _file(root / short[:3] / short[3:].lower() / "plan.md", data)
    _file(root / "plain" / "plan.md", b"plain\n")
    res, _ = _run([root])
    held = _by_name(res, short[3:].lower() + "/plan.md")[0]
    assert held["path"] == inventory.WITHHELD and held["group"] == inventory.WITHHELD and held["rank"] == ""
    assert held["suffix"] == ".md" and held["size"] == str(len(data))
    assert held["sha256"] == hashlib.sha256(data).hexdigest() and held["md5"] == hashlib.md5(data).hexdigest()
    assert _by_name(res, "plain/plan.md")[0]["path"] == "plain/plan.md", "a plain path is kept as it is"
    assert res.withheld == 1
    assert short[:3] not in res.export.read_text(encoding="utf-8")


def test_r3_a_label_with_a_name_is_refused_and_a_plain_one_names_the_file(lap):
    root = lap / "root"
    _file(root / "a.txt")
    with pytest.raises(cleaninventory.InventoryError, match="label"):
        _run([root], label=fx.CUSTOMER_FORMS[1].lower())
    res, _ = _run([root], label="kunden")
    assert res.export.name == "laptop-20261029-101500-kunden.tsv"


# --------------------------------------------------------------------------- the real paths stay local


def test_r3_the_real_path_map_stays_on_the_laptop(lap):
    root = lap / "win" / "c" / "users" / "someone" / "work"
    _file(root / "projects" / "plan.xlsx", b"x" * 10)
    with FakeOBS() as bkt:
        res, _ = _run([root], project=PROJECT, put=True, client=bkt.client())
        objects = dict(bkt.objects)
        calls = list(bkt.calls)
    assert res.local.parent == laptop.vault() / "inventory"
    assert oct(res.local.stat().st_mode & 0o777) == "0o600"
    local = inventory.read(res.local)
    assert local[0]["real path"] == str(root / "projects" / "plan.xlsx") and local[0]["root"] == str(root)
    assert list(objects) == [inventory.PREFIX + res.export.name], "only the coded copy goes up"
    assert [m for m, _ in calls if m == "PUT"] == ["PUT"]
    sent = objects[inventory.PREFIX + res.export.name].decode("utf-8")
    assert sent == res.export.read_text(encoding="utf-8")
    assert sent.splitlines()[0] == inventory.HEADER
    assert str(lap) not in sent and "someone" not in sent and "real path" not in sent
    row = inventory.read(res.export)[0]
    assert row["path"] == "projects/plan.xlsx" and row["project"] == PROJECT and row["place"] == "laptop"
    assert row["sha256"] == hashlib.sha256(b"x" * 10).hexdigest() and row["size"] == "10"


def test_r3_a_second_put_of_the_same_name_is_not_replaced(lap):
    root = lap / "root"
    _file(root / "a.txt")
    with FakeOBS() as bkt:
        res, _ = _run([root], put=True, client=bkt.client())
        bkt.objects[inventory.PREFIX + res.export.name] = b"kept"
        with pytest.raises(cleaninventory.InventoryError, match="not replaced"):
            cleaninventory.put_export(res.export, client=bkt.client())
        assert bkt.objects[inventory.PREFIX + res.export.name] == b"kept"


# --------------------------------------------------------------------------- what is skipped


def _workbook(path: Path, sheets) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.title = sheets[0]
    for name in sheets[1:]:
        wb.create_sheet(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def test_r3_the_report_folder_the_workbook_and_the_noise_are_skipped(lap):
    root = lap / "root"
    _file(root / "awb-report" / "projects.csv")
    _workbook(root / "copies" / "status.xlsx", ("Projects", "Names", "Status"))
    other = _workbook(root / "copies" / "prices.xlsx", ("Projects", "Prices"))
    _file(root / "_archive" / "old.txt")
    _file(root / ".git" / "config")
    _file(root / "~$offer.docx")
    _file(root / "scratch.tmp")
    _file(root / "Thumbs.db")
    _file(root / "desktop.ini")
    keep = _file(root / "keep.txt")
    (root / "link.txt").symlink_to(keep)
    res, _ = _run([root])
    listed = sorted(r["real path"] for r in res.rows)
    assert listed == sorted([str(keep), str(other)])
    assert res.skipped["the report folder"] == 1 and res.skipped["report workbooks"] == 1
    assert res.skipped["_archive and .git folders"] == 2 and res.skipped["links"] == 1
    assert res.skipped["office lock files"] == 1 and res.skipped[".tmp files"] == 1
    assert res.skipped["Thumbs.db and desktop.ini"] == 2


def test_r3_a_root_in_the_report_folder_or_under_onedrive_is_refused(lap):
    report = lap / "awb-report"
    _file(report / "projects.csv")
    with pytest.raises(cleaninventory.InventoryError, match="never leaves the laptop"):
        _run([report])
    cloud = lap / "OneDrive - firm" / "docs"
    _file(cloud / "a.txt")
    with pytest.raises(cleaninventory.InventoryError, match="OneDrive"):
        _run([cloud])
    res, _ = _run([cloud], include_onedrive=True)
    assert len(res.rows) == 1


def test_r3_the_laptop_folder_itself_is_never_listed(lap):
    _file(lap / "elsewhere" / "a.txt")
    res, _ = _run([lap])
    assert all(not r["real path"].startswith(str(laptop.home())) for r in res.rows)
    assert res.skipped["the laptop folder"] == 1 and len(res.rows) == 1


# --------------------------------------------------------------------------- groups, ranks, several roots


def test_r3_versions_share_a_group_and_rank_newest_first(lap):
    root = lap / "root"
    _file(root / "offer v1.docx", b"one", mtime=1_700_000_000)
    _file(root / "offer v2.docx", b"two", mtime=1_700_100_000)
    _file(root / "other.docx", b"three", mtime=1_700_000_000)
    res, _ = _run([root], project=PROJECT)
    v1, v2 = _by_name(res, "offer v1.docx")[0], _by_name(res, "offer v2.docx")[0]
    assert v1["group"] == v2["group"] == PROJECT + "/offer.docx"
    assert (v2["rank"], v1["rank"]) == ("1", "2")
    assert _by_name(res, "other.docx")[0]["rank"] == "1"
    assert v2["mtime"] == "2023-11-16T02:00:00Z"
    assert [r["id"] for r in res.rows] == ["L-20261029-101500-%04d" % n for n in (1, 2, 3)]


def test_r3_several_roots_number_their_paths_and_hide_the_roots(lap):
    one, two = lap / "first", lap / "second"
    _file(one / "a.txt")
    _file(two / "a.txt")
    res, _ = _run([one, two])
    assert sorted(r["path"] for r in inventory.read(res.export)) == ["root1/a.txt", "root2/a.txt"]


# --------------------------------------------------------------------------- the command


def test_r3_the_command_reads_a_windows_path_and_puts(lap, monkeypatch, capsys):
    root = lap / "mnt" / "d" / "kunden"
    _file(root / "a.txt")
    monkeypatch.setattr(clean, "windows_path", lambda s: str(root) if s.startswith("D:") else s)
    with FakeOBS() as bkt:
        monkeypatch.setattr(clean, "_client", lambda what="--put": bkt.client())
        assert cli.main(["clean", "inventory", "D:\\Kunden", "--project", PROJECT, "--put"]) == 0
        keys = list(bkt.objects)
    out = capsys.readouterr().out
    assert "inventory: 1 file, 0 paths withheld" in out and "bucket: reports/inventory/laptop-" in out
    assert len(keys) == 1 and keys[0].startswith(inventory.PREFIX)
    assert str(root) not in out
    assert cli.main(["clean", "inventory", str(lap / "missing")]) == 2
    assert cli.main(["clean", "inventory", str(root), "--project", "nonsense"]) == 2
