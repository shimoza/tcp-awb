"""Task R4 of presentations/reports-design: `awb files inventory --host|--buckets` lists the files of the project
folders and of the two buckets with sizes, times and hashes and never their content, in the rows of the shared
module awb/inventory.py (task R3) that the move plan (R5) reads. Invented projects in a temporary Workbench (the
`home` fixture), a real key service in threads and two stand-in buckets of tests/obs_fake.py, the invented names of
tests/fixtures.py only."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
from pathlib import Path

import pytest

from awb import bucket, cli, filesinv, inventory, projects
from awb.tcp import keys
from tests import fixtures
from tests.obs_fake import FakeOBS
from tests.test_bucket_keys import AK, SK, STORE, quiet, short_dir  # noqa: F401

NAME = fixtures.CUSTOMER_FORMS[0]
LAB = "awb-lab"


def _project(home, code: str, state: str = "active", created: str = "2026-10-05") -> projects.Project:
    folder = home.projects_root / code
    folder.mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# Scope of %s\n\n- goal: a lab run\n" % code, encoding="utf-8")
    return projects.Project(code, "project", fixtures.CUSTOMER_CODE, "tcp", str(folder), projects.memory_key(folder),
                            state, created)


@pytest.fixture
def planted(home):
    rows = [_project(home, "tcp-q7m4"), _project(home, "tcp-b3np", "closed"), _project(home, "tcp-wqyz", "deleted")]
    projects._save(home, rows)
    return home


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_r4_the_host_and_the_buckets_write_the_shared_rows_of_r3(planted, tmp_path):
    """No row format of its own: the columns, the header, the ids and the file names are those of awb/inventory.py."""
    when = inventory.stamp(dt.datetime(2026, 10, 29, 17, 50, tzinfo=dt.timezone.utc))
    out = tmp_path / "host.tsv"
    files, n, _, _ = filesinv.make(planted, "host", out, now=dt.datetime(2026, 10, 29, 17, 50, tzinfo=dt.timezone.utc))
    assert files == [out] and n == 2
    assert out.read_text(encoding="utf-8").splitlines()[0] == inventory.HEADER
    rows = inventory.read(out)
    assert [r["id"] for r in rows] == [inventory.row_id("host", when, 1), inventory.row_id("host", when, 2)]
    assert not hasattr(filesinv, "COLUMNS") and not hasattr(filesinv, "HEADER")


def test_r4_an_etag_is_the_md5_of_a_single_part_object_only():
    md5 = hashlib.md5(b"x").hexdigest()
    assert filesinv.md5_of_etag('"%s"' % md5) == md5
    assert filesinv.md5_of_etag('"%s-3"' % md5) == ""
    assert filesinv.utc_text("2026-10-03T07:58:12.000Z") == "2026-10-03T07:58:12Z"
    assert filesinv.utc_text("not a time") == ""


def test_r4_host_lists_every_project_file_with_hashes_and_withholds_a_name(planted, tmp_path, capsys):
    a = planted.projects_root / "tcp-q7m4"
    (a / "deliverables").mkdir()
    (a / "deliverables" / "offer v1.md").write_bytes(b"one\n")
    (a / "deliverables" / "offer v2.md").write_bytes(b"two\n")
    (a / ("notes for %s.md" % NAME)).write_bytes(b"x\n")
    (a / ".git").mkdir()
    (a / ".git" / "HEAD").write_bytes(b"ref\n")
    (a / "~$offer.docx").write_bytes(b"lock")
    (a / "outside").symlink_to(tmp_path)
    (a / "link.md").symlink_to(a / "SCOPE.md")
    old = dt.datetime(2026, 10, 1, 8, 0, tzinfo=dt.timezone.utc).timestamp()
    os.utime(a / "deliverables" / "offer v1.md", (old, old))
    (planted.projects_root / "tcp-b3np" / "closed.md").write_bytes(b"c\n")
    (planted.projects_root / "tcp-wqyz" / "gone.md").write_bytes(b"d\n")
    out = tmp_path / "host.tsv"
    code, stdout, err = run(["files", "inventory", "--host", "--out", str(out)], capsys)
    assert code == 0, err
    assert "6 files of the host, 1 path withheld" in stdout
    rows = inventory.read(out)
    assert sorted((r["project"], r["path"]) for r in rows) == [
        ("tcp-b3np", "SCOPE.md"), ("tcp-b3np", "closed.md"), ("tcp-q7m4", "SCOPE.md"), ("tcp-q7m4", "[withheld]"),
        ("tcp-q7m4", "deliverables/offer v1.md"), ("tcp-q7m4", "deliverables/offer v2.md")]
    by_path = {r["path"]: r for r in rows}
    v1, v2 = by_path["deliverables/offer v1.md"], by_path["deliverables/offer v2.md"]
    assert v1["sha256"] == hashlib.sha256(b"one\n").hexdigest() and v1["md5"] == hashlib.md5(b"one\n").hexdigest()
    assert (v1["size"], v1["suffix"], v1["place"], v1["mtime"]) == ("4", ".md", "host", "2026-10-01T08:00:00Z")
    assert v1["group"] == v2["group"] == "tcp-q7m4/offer.md" and (v2["rank"], v1["rank"]) == ("1", "2")
    hidden = by_path["[withheld]"]
    assert hidden["sha256"] == hashlib.sha256(b"x\n").hexdigest() and hidden["size"] == "2"
    assert (hidden["project"], hidden["suffix"], hidden["group"], hidden["rank"]) == ("tcp-q7m4", ".md", "[withheld]", "")
    assert all(r["id"].startswith("H-") for r in rows) and len({r["id"] for r in rows}) == 6
    fixtures.assert_no_fixture_name(out.read_text(encoding="utf-8") + stdout + err, "the host inventory")


def test_r4_host_writes_to_the_shared_files_folder_by_default(planted, capsys):
    code, stdout, err = run(["files", "inventory", "--host"], capsys)
    assert code == 0, err
    found = list((planted.shared / "files").iterdir())
    assert len(found) == 1 and found[0].name.startswith("host-") and found[0].suffix == ".tsv"
    assert str(found[0]) in stdout and [r["path"] for r in inventory.read(found[0])] == ["SCOPE.md", "SCOPE.md"]


def test_r4_nothing_is_written_when_the_names_cannot_be_checked(planted, tmp_path, capsys):
    (planted.projects_root / "tcp-q7m4" / "a.md").write_bytes(b"a\n")
    planted.register.unlink()
    out = tmp_path / "host.tsv"
    code, _, err = run(["files", "inventory", "--host", "--out", str(out)], capsys)
    assert code == 2 and "nothing is written" in err and not out.exists()


def test_r4_one_of_host_or_buckets_is_required(planted, capsys):
    assert run(["files", "inventory"], capsys)[0] == 2
    assert run(["files", "inventory", "--host", "--buckets"], capsys)[0] == 2


def test_r4_a_withheld_path_keeps_a_known_suffix_only(home):
    rows = [filesinv._row("a\tb.md", 1, "", "", "", "", "host"),
            filesinv._row("plain.md", 1, "", "", "", "", "host"),
            filesinv._row("%s.%s" % ("x", fixtures.CUSTOMER_FORMS[1].lower()[:8]), 1, "", "", "", "", "host")]
    assert filesinv.check(rows, home.register) == 2
    assert [(r["path"], r["suffix"]) for r in rows] == [("[withheld]", ".md"), ("plain.md", ".md"), ("[withheld]", "")]


# --------------------------------------------------------------------------- the buckets


@pytest.fixture
def two():
    with FakeOBS("awb", AK, SK) as own, FakeOBS(LAB, AK, SK) as lab:
        yield own, lab


@pytest.fixture
def svc(planted, two, tmp_path, monkeypatch):
    own, lab = two
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log",
                         obs_endpoints={own.bucket: own.endpoint, lab.bucket: lab.endpoint})
        s.start()
        monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(s.admin_path))
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        keys.unlock(s.admin_path, sorted(STORE), reader=STORE.get,
                    settings={"bucket_tenant": "test-1", "lab_bucket": LAB, "owner_bucket": own.bucket, "max_mb": "1"})
        try:
            yield s
        finally:
            s.stop()


def _fill(own, lab):
    base = "2026-10/tcp-q7m4/"
    own.objects.update({
        "2026-10/": b"", base: b"", base + "in/": b"", base + "in/.keep": b"keep", base + "out/": b"",
        base + "in/brief.txt": b"brief\n",
        base + "in/%s brief.txt" % NAME: b"named\n",
        base + "out/offer.md": b"offer\n",
        "2026-10/tcp-b3np/out/old.md": b"old\n",                    # a closed project: its folder is not listed
        "2026-10/somewhere/out/x.md": b"x\n",                       # a folder of no project
        "inbox/%s.pdf" % NAME: b"pdf\n",
        "inbox/plain.pdf": b"plain\n",
        "reports/projects.csv": b"csv\n",
        "datasets/tcp-facts/LATEST": b"d\n",
    })
    own.modified[base + "out/offer.md"] = "2026-10-20T09:30:00.000Z"
    lab.objects.update({
        "tcp-q7m4/from-session/2026-10-28/prj-0a1b2c3d4e5f": b"result\n",
        "tcp-q7m4/in/%s.docx" % NAME: b"doc\n",
        "inbox/scan.png": b"png\n",
        "tcp-zzzz/from-session/x": b"z\n",                          # no registered project
        "loose.txt": b"l\n",
    })
    return base


def test_r4_buckets_lists_both_buckets_with_etags_and_withholds_originals_with_a_name(svc, two, tmp_path, capsys):
    own, lab = two
    base = _fill(own, lab)
    out = tmp_path / "buckets.tsv"
    code, stdout, err = run(["files", "inventory", "--buckets", "--out", str(out)], capsys)
    assert code == 0, err
    rows = inventory.read(out)
    assert "8 files of the buckets, 3 paths withheld; 3 folders not listed (no project)" in stdout
    assert sorted((r["place"], r["path"]) for r in rows) == sorted([
        ("owner-bucket", base + "in/brief.txt"), ("owner-bucket", "[withheld]"), ("owner-bucket", base + "out/offer.md"),
        ("owner-bucket", "[withheld]"), ("owner-bucket", "inbox/plain.pdf"),
        ("lab-bucket", "tcp-q7m4/from-session/2026-10-28/prj-0a1b2c3d4e5f"), ("lab-bucket", "[withheld]"),
        ("lab-bucket", "inbox/scan.png")])
    offer = next(r for r in rows if r["path"].endswith("offer.md"))
    assert offer["md5"] == hashlib.md5(b"offer\n").hexdigest() and offer["sha256"] == ""
    assert (offer["size"], offer["mtime"], offer["project"]) == ("6", "2026-10-20T09:30:00Z", "tcp-q7m4")
    assert {r["project"] for r in rows if r["path"].startswith("inbox/")} == {inventory.NO_PROJECT}
    assert all(r["id"][0] == inventory.PLACES[r["place"]] for r in rows)
    fixtures.assert_no_fixture_name(out.read_text(encoding="utf-8") + stdout + err, "the bucket inventory")
    assert "somewhere" not in stdout + err


def test_r4_buckets_write_one_file_per_place_by_default(svc, two, planted, capsys):
    _fill(*two)
    code, stdout, err = run(["files", "inventory", "--buckets"], capsys)
    assert code == 0, err
    found = sorted((planted.shared / "files").iterdir())
    assert [f.name.split("-2")[0] for f in found] == ["lab-bucket", "owner-bucket"]
    for f in found:
        assert {r["place"] for r in inventory.read(f)} == {f.name.split("-2")[0]}
        assert str(f) in stdout


def test_r4_the_lab_listing_is_the_owners_and_keeps_to_the_layout(svc, two):
    own, lab = two
    lab.objects["tcp-q7m4/from-session/a"] = b"a"
    ask = lambda sock, **r: keys.request(sock, r)               # noqa: E731
    assert ask(svc.admin_path, op="lab_list", prefix="", delimiter="/")["ok"] is True
    assert ask(svc.admin_path, op="lab_list", prefix="tcp-q7m4/")["objects"][0][0] == "tcp-q7m4/from-session/a"
    assert ask(svc.admin_path, op="lab_list", prefix="inbox/")["ok"] is True
    for prefix, delim in (("", ""), ("loose", ""), ("../", ""), ("tcp-q7m4/", "x"), ("/tcp-q7m4/", "")):
        assert ask(svc.admin_path, op="lab_list", prefix=prefix, delimiter=delim)["ok"] is False, prefix
    assert ask(svc.call_path, op="lab_list", prefix="", delimiter="/")["ok"] is False
    assert ask(svc.admin_path, op="lab_get", key="tcp-q7m4/from-session/a")["ok"] is False
    assert "lab_list" in keys.ADMIN_OPS


def test_r4_the_owner_reads_a_laptop_inventory_of_reports_and_nothing_else_there(svc, two, tmp_path):
    """The plan (R5) reads the laptop inventories that `awb clean inventory --put` put under reports/inventory/: a
    get of the owner's CLI, never a drop, never another file of reports/, never through the call socket."""
    own, _ = two
    name = inventory.file_name("laptop", "20261029-101500")
    key = "reports/inventory/" + name
    own.objects[key] = (inventory.HEADER + "\n").encode()
    own.objects["reports/inventory/notes.txt"] = b"n\n"
    got = tmp_path / "got.tsv"
    bucket.client().get(key, got)
    assert got.read_bytes() == own.objects[key]
    labelled = "reports/inventory/" + inventory.file_name("laptop", "20261029-101500", "work")
    own.objects[labelled] = b"x"
    ask = lambda sock, **r: keys.request(sock, r)               # noqa: E731
    assert ask(svc.admin_path, op="bucket_get", key=labelled)["ok"] is True
    for other in ("reports/projects.csv", "reports/inventory/notes.txt", "reports/inventory/sub/" + name,
                  "reports/inventory/" + name + "x"):
        assert ask(svc.admin_path, op="bucket_get", key=other)["ok"] is False, other
    assert ask(svc.admin_path, op="bucket_drop_in", key=key)["ok"] is False and key in own.objects
    assert ask(svc.call_path, op="bucket_get", key=key)["ok"] is False
    put = tmp_path / "p.tsv"
    put.write_bytes(b"x")
    assert keys.request(svc.admin_path, {"op": "bucket_put", "key": key}, upload=put)["ok"] is False


def test_r4_a_locked_key_service_stops_the_bucket_inventory_with_one_line(svc, tmp_path, capsys):
    keys.request(svc.admin_path, {"op": "lock"})
    out = tmp_path / "b.tsv"
    code, _, err = run(["files", "inventory", "--buckets", "--out", str(out)], capsys)
    assert code == 2 and len(err.strip().splitlines()) == 1 and "awb keys unlock" in err and not out.exists()


def test_r4_the_work_user_gets_no_bucket_inventory(planted, monkeypatch, tmp_path, capsys):
    from awb import config

    monkeypatch.setattr(config, "is_work_user", lambda: True)
    code, _, err = run(["files", "inventory", "--buckets", "--out", str(tmp_path / "b.tsv")], capsys)
    assert code == 2 and "own shell" in err
