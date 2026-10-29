"""R6 of the report design: `awb clean layout`, the layout root in `awb clean use` and `awb clean fetch` and `awb
clean files plan|apply|undo` on the laptop. Invented names only, temporary folders and a fake bucket; the plan is
written by hand from the ids of a real `awb clean inventory` run."""
from __future__ import annotations

import hashlib
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awb import bucket_layout, clean, cleanfetch, cleanfiles, cleaninventory, cli, filesplan, laptop
from tests import fixtures as fx
from tests.obs_fake import FakeOBS

PROJECT = "tcp-q7m4"
MONTH = "2026-10"
STAMP = "20261029-110000"
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


def _file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _tree(*roots: Path) -> dict[str, str]:
    """Every file under the roots with its sha256 (empty folders do not count)."""
    out = {}
    for root in roots:
        for dirpath, _, files in os.walk(root):
            for f in files:
                p = Path(dirpath) / f
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _layout(lap: Path) -> Path:
    root = lap / "layout"
    assert cli.main(["clean", "layout", str(root)]) == 0
    return root


def _setup(lap: Path):
    """An inventory root with four files, the layout root and the inventory; returns (root, layout, id by name)."""
    root = lap / "files"
    _file(root / "offer.docx", b"offer, the newest\n")
    _file(root / "old" / "offer.docx", b"offer, an older version\n")
    _file(root / "notes.txt", b"my notes\n")
    _file(root / "keep.txt", b"stays\n")
    layout = _layout(lap)
    res = cleaninventory.run([root], project=PROJECT, now=NOW, say=lambda s: None)
    ids = {str(Path(r["real path"]).relative_to(root)): r["id"] for r in res.rows}
    return root, layout, ids


def _plan_text(rows) -> str:
    lines = ["\t".join(cleanfiles.PLAN_COLUMNS)]
    for rid, action, target in rows:
        lines.append("\t".join((rid, "laptop", action, target, PROJECT, "a/coded/path", "", "a reason")))
    return "\n".join(lines) + "\n"


def _standard_plan(ids) -> str:
    return _plan_text([
        (ids["offer.docx"], "move", "%s/%s/private/" % (MONTH, PROJECT)),
        (ids["notes.txt"], "move", "%s/%s/private/" % (MONTH, PROJECT)),
        (ids["old/offer.docx"], "archive", "%s/%s/_archive/%s/" % (MONTH, PROJECT, STAMP)),
        (ids["keep.txt"], "keep", ""),
    ])


def _fetch(text: str, stamp: str = STAMP):
    lines = []
    with FakeOBS() as bkt:
        bkt.objects[bucket_layout.plan_key(stamp)] = text.encode()
        bad = cleanfiles.show(None, client=bkt.client(), say=lines.append)
        calls = list(bkt.calls)
    return bad, lines, calls


def _apply(**kw):
    lines = []
    count = cleanfiles.apply(say=lines.append, **kw)
    return count, lines


# --------------------------------------------------------------------------- the format and the layout


def test_r6_the_plan_columns_are_those_of_awb_files_plan():
    assert cleanfiles.PLAN_COLUMNS == filesplan.COLUMNS
    assert bucket_layout.plan_key(STAMP) == "reports/plan/%s/PLAN.tsv" % STAMP


def test_r6_the_layout_root_is_set_once_and_replaced_only_with_save(lap, capsys, monkeypatch):
    assert cli.main(["clean", "layout"]) == 0
    assert "not set" in capsys.readouterr().out
    root = _layout(lap)
    assert laptop.layout_root() == root and root.is_dir()
    assert stat.S_IMODE(os.stat(laptop.home() / laptop.LAYOUT).st_mode) == 0o600
    other = lap / "other"
    assert cli.main(["clean", "layout", str(other)]) == 2
    assert "--save" in capsys.readouterr().err
    assert laptop.layout_root() == root
    assert cli.main(["clean", "layout", str(other), "--save"]) == 0
    assert laptop.layout_root() == other
    monkeypatch.setattr(clean, "windows_path", lambda t: str(lap / "drive-d" / "Work") if t == "D:\\Work" else t)
    (lap / "drive-d").mkdir()
    assert cli.main(["clean", "layout", "D:\\Work", "--save"]) == 0
    assert laptop.layout_root() == lap / "drive-d" / "Work"


def test_r6_the_layout_root_refuses_the_report_folder_and_the_laptop_folder(lap, capsys):
    (lap / "awb-report").mkdir()
    assert cli.main(["clean", "layout", str(lap / "awb-report")]) == 2
    assert cli.main(["clean", "layout", str(laptop.home() / "work")]) == 2
    assert cli.main(["clean", "layout", str(lap / "no" / "such" / "folder")]) == 2
    assert laptop.layout_root() is None


def test_r6_use_makes_the_project_folders_under_the_layout(lap, capsys):
    root = _layout(lap)
    assert cli.main(["clean", "use", PROJECT, "--month", MONTH]) == 0
    base = root / MONTH / PROJECT
    assert all((base / k).is_dir() for k in ("private", "clean", "out"))
    assert not (base / "_archive").exists() and not (lap / "work" / PROJECT).exists()
    assert "folder: %s" % (base / "clean") in capsys.readouterr().out
    assert laptop.work_dir(PROJECT) == base / "clean" and laptop.clean_dir(PROJECT) == base / "clean"
    assert laptop.project_of_dir(base / "private") == PROJECT
    assert laptop.resolve_project(None, cwd=base / "out") == (PROJECT, "the folder")
    assert cli.main(["clean", "use", PROJECT, "--month", "2026-11"]) == 0   # the folder found wins
    assert not (root / "2026-11").exists()


def test_r6_use_takes_the_month_of_the_bucket_folder(lap, monkeypatch, capsys):
    root = _layout(lap)
    monkeypatch.setenv("AWB_OBS_AK", "ak-fake")
    monkeypatch.setenv("AWB_OBS_SK", "sk-fake")
    with FakeOBS() as bkt:
        bkt.objects["2026-09/%s/" % PROJECT] = b""
        bkt.objects["2026-09/%s/in/" % PROJECT] = b""
        monkeypatch.setattr(clean, "_client", lambda what="--put": bkt.client())
        assert cli.main(["clean", "use", PROJECT]) == 0
    assert (root / "2026-09" / PROJECT / "clean").is_dir()
    assert "month: 2026-09" in capsys.readouterr().out


def test_r6_without_a_layout_root_use_and_the_work_folder_stay_as_before(lap):
    assert laptop.use(PROJECT) == lap / "work" / PROJECT
    assert laptop.clean_dir(PROJECT) == lap / "work" / PROJECT / "clean"


def test_r6_fetch_without_to_lands_in_out_of_the_project_in_the_layout(lap):
    root = _layout(lap)
    base = "%s/%s/" % (MONTH, PROJECT)
    with FakeOBS() as bkt:
        bkt.objects[base] = b""
        bkt.objects[base + "out/reply.md"] = b"dear colleague\n"
        lines = []
        assert cleanfetch.fetch(PROJECT, None, client=bkt.client(), say=lines.append) == 0
    assert (root / MONTH / PROJECT / "out" / "reply.md").read_bytes() == b"dear colleague\n"


# --------------------------------------------------------------------------- plan


def test_r6_plan_fetches_the_newest_and_prints_the_real_paths_and_sends_nothing(lap):
    root, layout, ids = _setup(lap)
    with FakeOBS() as bkt:
        bkt.objects[bucket_layout.plan_key("20261028-090000")] = _plan_text([]).encode()
        bkt.objects[bucket_layout.plan_key(STAMP)] = _standard_plan(ids).encode()
        lines = []
        assert cleanfiles.show(None, client=bkt.client(), say=lines.append) == 0
        methods = {m for m, _ in bkt.calls}
    assert methods <= {"GET"}
    text = "\n".join(lines)
    assert "plan %s: 2 move, 1 archive, 1 keep" % STAMP in text
    assert "%s -> %s" % (root / "offer.docx", layout / MONTH / PROJECT / "private" / "offer.docx") in text
    assert "%s -> %s" % (root / "old" / "offer.docx",
                         layout / MONTH / PROJECT / "_archive" / STAMP / "old" / "offer.docx") in text
    kept = cleanfiles.plan_path(STAMP)
    assert stat.S_IMODE(os.stat(kept).st_mode) == 0o600
    assert (root / "offer.docx").exists() and not (layout / MONTH).exists()


def test_r6_plan_names_an_unknown_id_and_a_bad_target(lap):
    root, layout, ids = _setup(lap)
    bad, lines, _ = _fetch(_plan_text([("L-20261029-101500-0099", "move", "%s/%s/private/" % (MONTH, PROJECT)),
                                       (ids["notes.txt"], "move", "%s/%s/../../" % (MONTH, PROJECT)),
                                       (ids["keep.txt"], "move", "%s/tcp-z9z9/private/" % MONTH)]))
    assert bad == 3
    text = "\n".join(lines)
    assert "not in the local inventory" in text and text.count("the target is not a folder of the layout") == 2


def test_r6_a_plan_with_another_header_is_refused(lap):
    _setup(lap)
    with FakeOBS() as bkt:
        bkt.objects[bucket_layout.plan_key(STAMP)] = b"id\tplace\taction\n"
        with pytest.raises(cleanfiles.FilesError):
            cleanfiles.show(None, client=bkt.client(), say=lambda s: None)
    assert not cleanfiles.plan_path(STAMP).exists()


# --------------------------------------------------------------------------- apply and undo


def test_r6_the_dry_run_changes_nothing(lap):
    root, layout, ids = _setup(lap)
    _fetch(_standard_plan(ids))
    before = _tree(root, layout)
    folders = sorted(str(p) for p in layout.rglob("*"))
    count, lines = _apply()
    assert count == {"moved": 3, "skipped": 0}
    assert _tree(root, layout) == before and sorted(str(p) for p in layout.rglob("*")) == folders
    assert not cleanfiles.journal_path(STAMP).exists()
    assert "dry run" in lines[-1] and "nothing changed" in lines[-1]


def test_r6_apply_then_undo_restores_the_exact_tree_and_deletes_nothing(lap):
    root, layout, ids = _setup(lap)
    _fetch(_standard_plan(ids))
    before = _tree(root, layout)
    count, lines = _apply(go=True)
    assert count == {"moved": 3, "skipped": 0}
    after = _tree(root, layout)
    assert sorted(after.values()) == sorted(before.values())          # nothing deleted, nothing changed
    base = layout / MONTH / PROJECT
    assert (base / "private" / "offer.docx").read_bytes() == b"offer, the newest\n"
    assert (base / "_archive" / STAMP / "old" / "offer.docx").read_bytes() == b"offer, an older version\n"
    assert (root / "keep.txt").exists() and not (root / "notes.txt").exists()
    journal = cleanfiles.journal_path(STAMP)
    assert stat.S_IMODE(os.stat(journal).st_mode) == 0o600
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 4
    lines = []
    assert cleanfiles.undo(say=lines.append) == {"back": 3, "skipped": 0}
    assert _tree(root, layout) == before
    assert not journal.exists() and list(journal.parent.glob(STAMP + ".undone-*.tsv"))


def test_r6_a_changed_or_missing_file_is_skipped(lap):
    root, layout, ids = _setup(lap)
    _fetch(_standard_plan(ids))
    (root / "offer.docx").write_bytes(b"offer, edited after the inventory\n")
    (root / "notes.txt").unlink()
    count, lines = _apply(go=True)
    assert count == {"moved": 1, "skipped": 2}
    text = "\n".join(lines)
    assert "changed since the inventory" in text and "missing" in text
    assert (root / "offer.docx").read_bytes() == b"offer, edited after the inventory\n"
    assert cli.main(["clean", "files", "apply", "--go"]) == 1


def test_r6_a_taken_name_gets_a_number_and_is_never_overwritten(lap):
    root, layout, ids = _setup(lap)
    _fetch(_standard_plan(ids))
    there = _file(layout / MONTH / PROJECT / "private" / "offer.docx", b"another file of that name\n")
    count, _ = _apply(go=True)
    assert count["moved"] == 3
    assert there.read_bytes() == b"another file of that name\n"
    assert (there.parent / "offer (2).docx").read_bytes() == b"offer, the newest\n"
    cleanfiles.undo(say=lambda s: None)
    assert (root / "offer.docx").read_bytes() == b"offer, the newest\n" and there.exists()


def test_r6_apply_needs_a_fetched_plan_and_a_layout_root(lap, capsys):
    with pytest.raises(cleanfiles.FilesError):
        cleanfiles.apply(say=lambda s: None)
    root, layout, ids = _setup(lap)
    _fetch(_standard_plan(ids))
    (laptop.home() / laptop.LAYOUT).unlink()
    assert cli.main(["clean", "files", "apply"]) == 2
    assert "awb clean layout" in capsys.readouterr().err
    assert cli.main(["clean", "files", "undo"]) == 2


def _step(src: Path, root: Path, dst: Path) -> cleanfiles.Step:
    data = src.read_bytes()
    return cleanfiles.Step("L-20261029-101500-0001", "move", src=src, dst=dst, root=root,
                           sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def test_r6_the_report_folder_the_workbook_onedrive_and_outside_places_are_never_touched(lap):
    openpyxl = pytest.importorskip("openpyxl")
    root, layout = lap / "files", _layout(lap)
    book = root / "status.xlsx"
    root.mkdir()
    wb = openpyxl.Workbook()
    wb.active.title = "Projects"
    wb.create_sheet("Names")
    wb.save(book)
    dst = layout / MONTH / PROJECT / "private"
    assert cleanfiles.check(_step(book, root, dst / "status.xlsx"), layout, False) == "a report workbook"
    report = _file(root / "awb-report" / "projects.csv", b"code\n")
    assert cleanfiles.check(_step(report, root, dst / "projects.csv"), layout, False) == "the report folder"
    plain = _file(root / "plain.txt", b"plain\n")
    assert cleanfiles.check(_step(plain, root, layout / "awb-report" / "plain.txt"), layout, False) \
        == "the report folder"
    assert cleanfiles.check(_step(plain, root, lap / "elsewhere" / "plain.txt"), layout, False) \
        == "its new place is outside the layout root"
    outside = _file(lap / "outside.txt", b"outside\n")
    assert cleanfiles.check(_step(outside, root, dst / "outside.txt"), layout, False) \
        == "outside the root of its inventory"
    cloud = _file(root / "OneDrive" / "cloud.txt", b"cloud\n")
    assert cleanfiles.check(_step(cloud, root, dst / "cloud.txt"), layout, False).startswith("under OneDrive")
    assert cleanfiles.check(_step(cloud, root, dst / "cloud.txt"), layout, True) == ""
    assert cleanfiles.check(_step(plain, root, dst / "plain.txt"), layout, False) == ""
    assert book.exists() and report.exists() and plain.exists() and cloud.exists()


def test_r6_the_commands_run_through_the_cli(lap, monkeypatch, capsys):
    root, layout, ids = _setup(lap)
    with FakeOBS() as bkt:
        bkt.objects[bucket_layout.plan_key(STAMP)] = _standard_plan(ids).encode()
        monkeypatch.setattr(clean, "_client", lambda what="--put": bkt.client())
        assert cli.main(["clean", "files", "plan"]) == 0
    assert cli.main(["clean", "files", "apply", STAMP]) == 0
    assert (root / "notes.txt").exists()
    assert cli.main(["clean", "files", "apply", STAMP, "--go"]) == 0
    assert not (root / "notes.txt").exists()
    assert cli.main(["clean", "files", "undo", STAMP]) == 0
    assert (root / "notes.txt").exists()
    assert cli.main(["clean", "files", "plan", "not-a-stamp"]) == 2
    assert cli.main(["clean", "files"]) == 2
