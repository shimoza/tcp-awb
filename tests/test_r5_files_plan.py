"""Task R5 of presentations/reports-design: `awb files plan` reads the coded inventories of the laptop, the host
and the buckets, matches identical files by hash, probable ones by size and time and versions by the normalised
stem, picks the keeper of each set and writes the plan: laptop moves into the layout and `_archive`, host and bucket
duplicates and stale objects of in/ listed only, nothing deleted. Invented inventories in a temporary Workbench (the
`home` fixture), a real key service in threads and a stand-in bucket of tests/obs_fake.py, the invented names of
tests/fixtures.py only."""
from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from awb import bucket_layout, cli, filesplan, inventory, projects
from awb.tcp import keys
from tests import fixtures
from tests.obs_fake import FakeOBS
from tests.test_bucket_keys import AK, SK, STORE, quiet, short_dir  # noqa: F401

NAME = fixtures.CUSTOMER_FORMS[0]
LAB = "awb-lab"
WHEN = "20261029-180000"
NOW = dt.datetime(2026, 10, 29, 18, 0, tzinfo=dt.timezone.utc)
A, B = "tcp-q7m4", "tcp-b3np"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class Inv:
    """Rows of an invented inventory, numbered per place as the inventories number them."""

    def __init__(self):
        self.rows: list[dict] = []
        self.count: dict[str, int] = {}

    def add(self, place: str, path: str, data: bytes | None, mtime: str = "2026-10-20T08:00:00Z",
            project: str = inventory.NO_PROJECT, *, size: int | None = None, md5_only: bool = False) -> str:
        self.count[place] = self.count.get(place, 0) + 1
        rid = inventory.row_id(place, "20261029-170000", self.count[place])
        name = path.rsplit("/", 1)[-1]
        suffix = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
        row = {"id": rid, "path": path, "suffix": suffix, "size": str(len(data) if data is not None else size),
               "mtime": mtime, "sha256": "" if data is None or md5_only else sha(data),
               "md5": "" if data is None else md5(data), "project": project, "group": "", "rank": "", "place": place}
        row["group"] = inventory.group_of(project, path, suffix)
        self.rows.append(row)
        return rid

    def write(self, path: Path, place: str | None = None) -> Path:
        path.write_text(inventory.text([r for r in self.rows if place is None or r["place"] == place]),
                        encoding="utf-8")
        return path


def _project(home, code: str, created: str = "2026-10-05") -> projects.Project:
    folder = home.projects_root / code
    folder.mkdir(parents=True)
    return projects.Project(code, "project", fixtures.CUSTOMER_CODE, "tcp", str(folder), projects.memory_key(folder),
                            "active", created)


@pytest.fixture
def planted(home):
    projects._save(home, [_project(home, A), _project(home, B, "2026-09-12")])
    return home


def make_plan(planted, inv: Inv, tmp_path: Path, **kw) -> tuple[dict, list[dict]]:
    lap = inv.write(tmp_path / "laptop-20261029-170000.tsv", "laptop")
    host = inv.write(tmp_path / "host-20261029-170000.tsv", "host")
    own = inv.write(tmp_path / "owner-bucket-20261029-170000.tsv", "owner-bucket")
    lab = inv.write(tmp_path / "lab-bucket-20261029-170000.tsv", "lab-bucket")
    res = filesplan.make(planted, [str(lap)], host, [own, lab], now=NOW, **kw)
    rows = [dict(zip(filesplan.COLUMNS, ln.split("\t")))
            for ln in (res["folder"] / "PLAN.tsv").read_text(encoding="utf-8").splitlines()[1:]]
    return res, rows


def by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["id"]: r for r in rows}


def test_r5_the_keeper_of_identical_laptop_copies_is_the_one_in_its_layout_place(planted, tmp_path):
    inv = Inv()
    deep = inv.add("laptop", "work/old/deep/brief.md", b"brief\n", "2026-10-21T08:00:00Z", A)
    placed = inv.add("laptop", "2026-10/tcp-q7m4/private/sub/brief.md", b"brief\n", "2026-10-01T08:00:00Z", A)
    flat = inv.add("laptop", "brief.md", b"brief\n", "2026-10-22T08:00:00Z", A)
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert got[placed]["action"] == "keep" and got[placed]["reason"] == "in its layout place"
    for rid in (deep, flat):
        assert got[rid]["action"] == "archive" and got[rid]["keeper"] == placed
        assert got[rid]["target"] == "2026-10/tcp-q7m4/_archive/%s/" % WHEN and got[rid]["reason"] == "identical"


def test_r5_else_the_shallowest_path_else_the_newest(planted, tmp_path):
    inv = Inv()
    deep = inv.add("laptop", "a/b/c/notes.txt", b"n\n", "2026-10-23T08:00:00Z", A)
    shallow = inv.add("laptop", "a/notes.txt", b"n\n", "2026-10-01T08:00:00Z", A)
    old = inv.add("laptop", "x/y.txt", b"y\n", "2026-10-01T08:00:00Z", A)
    new = inv.add("laptop", "z/y.txt", b"y\n", "2026-10-02T08:00:00Z", A)
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert got[deep]["keeper"] == shallow and got[shallow]["action"] == "move"
    assert got[shallow]["target"] == "2026-10/tcp-q7m4/private/"
    assert got[old]["action"] == "archive" and got[old]["keeper"] == new


def test_r5_a_laptop_file_without_a_project_takes_the_project_of_its_host_copy(planted, tmp_path):
    inv = Inv()
    loose = inv.add("laptop", "downloads/offer.md", b"offer\n")
    inv.add("host", "deliverables/offer.md", b"offer\n", project=A)
    up = inv.add("laptop", "stick/input.md", b"in\n")
    inv.add("owner-bucket", "2026-10/tcp-b3np/in/input.md", b"in\n", project=B, md5_only=True)
    both = inv.add("laptop", "mixed.md", b"both\n")
    inv.add("host", "input/mixed.md", b"both\n", project=A)
    inv.add("host", "input/mixed.md", b"both\n", project=B)
    alone = inv.add("laptop", "alone.md", b"alone\n")
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert (got[loose]["project"], got[loose]["action"], got[loose]["target"]) == (A, "move", "2026-10/tcp-q7m4/out/")
    assert (got[up]["project"], got[up]["target"]) == (B, "2026-10/tcp-b3np/clean/")
    assert got[both]["project"] == inventory.NO_PROJECT and got[both]["action"] == "keep"
    assert got[both]["reason"] == "identical to copies of more than one project"
    assert (got[alone]["action"], got[alone]["reason"], got[alone]["target"]) == ("keep", "no project", "")


def test_r5_older_versions_go_to_the_archive_and_the_newest_into_its_kind(planted, tmp_path):
    inv = Inv()
    v1 = inv.add("laptop", "kunde/angebot v1.docx", b"one", "2026-10-02T08:00:00Z", A)
    v2 = inv.add("laptop", "kunde/angebot final.docx", b"two", "2026-10-09T08:00:00Z", A)
    v3 = inv.add("laptop", "kunde/angebot 2026-10-12.docx", b"three", "2026-10-12T08:00:00Z", A)
    other = inv.add("laptop", "kunde/angebot.pdf", b"pdf", "2026-10-01T08:00:00Z", A)
    went_up = inv.add("laptop", "clean/angebot v1.docx", b"up", "2026-10-01T08:00:00Z", A)
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert got[v3]["action"] == "move" and got[v3]["target"] == "2026-10/tcp-q7m4/private/"
    for rid in (v1, v2):
        assert (got[rid]["action"], got[rid]["keeper"], got[rid]["reason"]) == ("archive", v3, "older version")
    assert got[other]["action"] == "move", "another suffix is another group"
    assert got[went_up]["action"] == "move" and got[went_up]["target"] == "2026-10/tcp-q7m4/clean/"


def test_r5_the_month_is_that_of_the_bucket_folder_else_of_the_register(planted, tmp_path):
    inv = Inv()
    a = inv.add("laptop", "a.md", b"a\n", project=A)
    inv.add("owner-bucket", "2026-11/tcp-q7m4/out/x.md", b"x\n", project=A, md5_only=True)
    b = inv.add("laptop", "b.md", b"b\n", project=B)
    c = inv.add("laptop", "c.md", b"c\n", project="tcp-zz99")
    dup = inv.add("laptop", "d/c.md", b"c\n", project="tcp-zz99")
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert got[a]["target"] == "2026-11/tcp-q7m4/private/" and got[b]["target"] == "2026-09/tcp-b3np/private/"
    assert (got[c]["action"], got[c]["reason"]) == ("keep", "no month for the project")
    assert got[dup]["action"] == "archive" and got[dup]["target"] == "_archive/%s/" % WHEN


def test_r5_host_and_buckets_are_listed_and_never_moved(planted, tmp_path):
    inv = Inv()
    stale = inv.add("owner-bucket", "2026-10/tcp-q7m4/in/brief.txt", b"brief\n", project=A, md5_only=True)
    hidden = inv.add("owner-bucket", inventory.WITHHELD, b"named\n", project=A, md5_only=True)
    out1 = inv.add("owner-bucket", "2026-10/tcp-q7m4/out/offer.md", b"offer\n", "2026-10-02T08:00:00Z", A,
                   md5_only=True)
    out2 = inv.add("owner-bucket", "2026-10/tcp-q7m4/out/offer-copy.md", b"offer\n", "2026-10-03T08:00:00Z", A,
                   md5_only=True)
    s1 = inv.add("lab-bucket", "tcp-q7m4/from-session/2026-10-27/r.md", b"r\n", project=A, md5_only=True)
    s2 = inv.add("lab-bucket", "tcp-q7m4/from-session/2026-10-28/r.md", b"r\n", project=A, md5_only=True)
    h1 = inv.add("host", "deliverables/plan.md", b"p\n", project=A)
    h2 = inv.add("host", "deliverables/old/plan.md", b"p\n", project=A)
    other = inv.add("host", "deliverables/plan.md", b"p\n", project=B)
    lone = inv.add("host", "SCOPE.md", b"s\n", project=A)
    inbox = inv.add("owner-bucket", "inbox/plain.pdf", b"pdf\n", md5_only=True)
    res, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert got[stale]["action"] == "stale-in" and got[hidden]["action"] == "withheld"
    assert (got[out1]["action"], got[out1]["keeper"]) == ("duplicate", out2) and out2 not in got, "the newest stays"
    assert got[s2]["action"] == "duplicate" and got[s2]["keeper"] == s1
    assert (got[h2]["action"], got[h2]["keeper"]) == ("duplicate", h1)
    assert other not in got and lone not in got and inbox not in got, "another project, one copy, the inbox"
    assert all(r["target"] == "" for r in rows) and {r["action"] for r in rows} == {"stale-in", "withheld", "duplicate"}
    assert res["listed"] == len(rows) and res["laptop"] == {"keep": 0, "move": 0, "archive": 0}


def test_r5_a_multipart_object_is_a_probable_copy_listed_only(planted, tmp_path):
    inv = Inv()
    lap = inv.add("laptop", "big.zip", b"z" * 50, "2026-10-20T08:00:00Z", A)
    obj = inv.add("owner-bucket", "2026-10/tcp-q7m4/out/big.zip", None, "2026-10-20T08:01:30Z", A, size=50)
    far = inv.add("owner-bucket", "2026-10/tcp-q7m4/out/big2.zip", None, "2026-10-20T08:05:00Z", A, size=50)
    res, rows = make_plan(planted, inv, tmp_path)
    assert res["pairs"] == 1 and by_id(rows)[lap]["action"] == "move"
    text = (res["folder"] / "PLAN.md").read_text(encoding="utf-8")
    probable = text.split("## Probable copies")[1]
    assert lap in probable and obj in probable and far not in probable


def test_r5_empty_files_match_nothing(planted, tmp_path):
    inv = Inv()
    one = inv.add("laptop", "a/empty.txt", b"", project=A)
    two = inv.add("laptop", "b/blank.txt", b"", project=A)
    _, rows = make_plan(planted, inv, tmp_path)
    assert {by_id(rows)[one]["action"], by_id(rows)[two]["action"]} == {"move"}


def test_r5_a_laptop_name_is_withheld_at_entry_and_a_bad_row_is_dropped(planted, tmp_path):
    inv = Inv()
    named = inv.add("laptop", "kunden/%s/brief.md" % NAME, b"brief\n", project=A)
    inv.add("laptop", "good.md", b"g\n", project=A)
    inv.rows.append(dict(inv.rows[-1], id="X-1", path="bad.md"))
    inv.rows.append(dict(inv.rows[-2], id=inventory.row_id("laptop", "20261029-170000", 9), md5="nothex"))
    res, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert got[named]["path"] == inventory.WITHHELD and got[named]["action"] == "move"
    assert len(rows) == 2
    md = (res["folder"] / "PLAN.md").read_text(encoding="utf-8")
    assert "1 laptop path withheld at entry" in md and "2 rows dropped" in md
    fixtures.assert_no_fixture_name(md + (res["folder"] / "PLAN.tsv").read_text(encoding="utf-8"), "the plan")


def test_r5_the_plan_files_and_their_format(planted, tmp_path):
    inv = Inv()
    keep = inv.add("laptop", "2026-10/tcp-q7m4/out/r.md", b"r\n", project=A)
    inv.add("laptop", "r.md", b"r\n", project=A)
    inv.add("owner-bucket", "2026-10/tcp-q7m4/in/x.txt", b"x\n", project=A, md5_only=True)
    res, rows = make_plan(planted, inv, tmp_path)
    folder = res["folder"]
    assert folder == planted.shared / "files" / ("plan-%s" % WHEN)
    assert sorted(f.name for f in folder.iterdir()) == ["PLAN.md", "PLAN.tsv"]
    lines = (folder / "PLAN.tsv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == filesplan.HEADER == "id\tplace\taction\ttarget\tproject\tpath\tkeeper\treason"
    assert [r["place"] for r in rows] == ["laptop", "laptop", "owner-bucket"], "laptop rows first"
    assert all(r["target"].endswith("/") for r in rows if r["action"] in ("move", "archive"))
    md = (folder / "PLAN.md").read_text(encoding="utf-8")
    assert "## tcp-q7m4 (month 2026-10)" in md and keep in md and "Nothing is deleted anywhere" in md
    for f in tmp_path.glob("*.tsv"):
        assert f.read_text(encoding="utf-8").splitlines()[0] == inventory.HEADER, "the inventories stay as they were"
    with pytest.raises(filesplan.PlanError):
        make_plan(planted, inv, tmp_path)


def test_r5_no_inventory_at_all_is_refused(planted):
    with pytest.raises(filesplan.PlanError):
        filesplan.make(planted, [], None, None, now=NOW)


# --------------------------------------------------------------------------- the command and the bucket


@pytest.fixture
def own():
    with FakeOBS("awb", AK, SK) as o, FakeOBS(LAB, AK, SK) as lab:
        yield o, lab


@pytest.fixture
def svc(planted, own, tmp_path, monkeypatch):
    o, lab = own
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log",
                         obs_endpoints={o.bucket: o.endpoint, lab.bucket: lab.endpoint})
        s.start()
        monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(s.admin_path))
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        keys.unlock(s.admin_path, sorted(STORE), reader=STORE.get,
                    settings={"bucket_tenant": "test-1", "lab_bucket": LAB, "owner_bucket": o.bucket, "max_mb": "1"})
        try:
            yield s
        finally:
            s.stop()


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_r5_the_command_reads_the_newest_inventories_and_puts_the_laptop_part(svc, own, planted, capsys):
    o, _ = own
    old, new = Inv(), Inv()
    old.add("laptop", "old.md", b"old\n", project=A)
    moved = new.add("laptop", "brief.md", b"brief\n", project=A)
    extra = new.add("laptop", "copy/brief.md", b"brief\n", project=A)
    new.add("host", "input/brief.md", b"brief\n", project=A)
    stale = new.add("owner-bucket", "2026-10/tcp-q7m4/in/brief.md", b"brief\n", project=A, md5_only=True)
    o.objects["reports/inventory/laptop-20261028-090000.tsv"] = inventory.text(old.rows).encode()
    o.objects["reports/inventory/laptop-20261029-170000-work.tsv"] = inventory.text(
        [r for r in new.rows if r["place"] == "laptop"]).encode()
    files = planted.shared / "files"
    files.mkdir()
    new.write(files / "host-20261029-170000.tsv", "host")
    new.write(files / "owner-bucket-20261029-170000.tsv", "owner-bucket")
    (files / "host-20261001-170000.tsv").write_text(inventory.HEADER + "\n", encoding="utf-8")
    code, out, err = run(["files", "plan", "--put"], capsys)
    assert code == 0, err
    assert "laptop 0 keep, 1 move, 1 archive (0 without a project); 1 host and bucket row listed" in out
    assert "no bucket inventory" not in out and "nothing moved, nothing deleted" in out
    folder = next(files.glob("plan-*"))
    stamp = folder.name[len("plan-"):]
    key = bucket_layout.plan_key(stamp)
    assert key == "reports/plan/%s/PLAN.tsv" % stamp and key in out
    put = o.objects[key].decode("utf-8").splitlines()
    assert put[0] == filesplan.HEADER and len(put) == 3
    got = {ln.split("\t")[0]: ln.split("\t") for ln in put[1:]}
    assert got[moved][2:4] == ["move", "2026-10/tcp-q7m4/clean/"] and got[extra][2] == "archive"
    assert stale not in got and stale in (folder / "PLAN.tsv").read_text(encoding="utf-8")
    assert "laptop-20261029-170000-work.tsv" in (folder / "PLAN.md").read_text(encoding="utf-8")
    assert [k for k in o.objects if k.startswith("reports/plan/")] == [key]


def test_r5_without_a_laptop_inventory_in_the_bucket_one_line(svc, planted, capsys):
    code, _, err = run(["files", "plan"], capsys)
    assert code == 2 and len(err.strip().splitlines()) == 1 and "awb clean inventory --put" in err


def test_r5_a_local_laptop_file_needs_no_bucket_and_missing_places_are_said(planted, tmp_path, capsys):
    inv = Inv()
    inv.add("laptop", "a.md", b"a\n", project=A)
    lap = inv.write(tmp_path / "mine.tsv")
    code, out, err = run(["files", "plan", "--laptop", str(lap)], capsys)
    assert code == 0, err
    assert "no host inventory" in out and "no bucket inventory" in out and "1 move" in out


def test_r5_the_owner_may_put_the_laptop_part_of_a_plan_and_nothing_else_there(svc, own, tmp_path):
    o, _ = own
    f = tmp_path / "p.tsv"
    f.write_bytes(b"x\n")
    ask = lambda sock, key: keys.request(sock, {"op": "bucket_put", "key": key}, upload=f)    # noqa: E731
    key = bucket_layout.plan_key(WHEN)
    assert ask(svc.admin_path, key)["ok"] is True and o.objects[key] == b"x\n"
    for other in ("reports/plan/%s/PLAN.md" % WHEN, "reports/plan/PLAN.tsv", "reports/plan/2026-10-29/PLAN.tsv",
                  "reports/plan/%s/sub/PLAN.tsv" % WHEN, "reports/plan/%s/PLAN.tsvx" % WHEN,
                  "reports/inventory/laptop-%s.tsv" % WHEN):
        assert ask(svc.admin_path, other)["ok"] is False, other
    assert ask(svc.call_path, bucket_layout.plan_key("20261029-180001"))["ok"] is False
    assert keys.request(svc.admin_path, {"op": "bucket_get", "key": key})["ok"] is False, "a put, never a read"
