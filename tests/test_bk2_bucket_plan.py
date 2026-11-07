"""Task BK2 of presentations/buckets: the bucket rows of `awb files plan` get an action (keep, move, unsorted,
archive, delete, withheld), a target key where they have one and the size of the object. The plan never moves or
deletes anything. Inventory rows built in the test (the `Inv` of tests/test_r5_files_plan.py), never a real bucket;
the rows of INDEX.md and the record of what the imports took are given to `filesplan.make`, or written into the
temporary vault of the `home` fixture. Codes only."""
from __future__ import annotations

import re
import shutil

from awb import bucket, cleanfiles, cli, filesplan, inventory
from tests.test_r5_files_plan import A, B, WHEN, Inv, by_id, make_plan, md5, planted  # noqa: F401

PUT = "tcp-q7m4/from-session/2026-09-20/doc/tcp-q7m4-doc-en-1010.md"
INDEX_KEY = "tcp-q7m4/from-session/INDEX.md"


def index_row(key: str, fetched: str) -> dict:
    date = key.split("/")[2]
    return {"date": date, "time": "10:10", "key": key, "kind": key.split("/")[3], "lang": "en", "size": "3",
            "review": "none", "fetched": fetched}


def lab_put(inv: Inv, key: str = PUT, data: bytes = b"put") -> str:
    return inv.add("lab-bucket", key, data, project=A, md5_only=True)


# --------------------------------------------------------------------------- each action on one object


def test_bk2_keep_an_object_in_its_layout_place(planted, tmp_path):
    inv = Inv()
    out = inv.add("owner-bucket", "2026-10/tcp-q7m4/out/offer.md", b"offer\n", project=A, md5_only=True)
    arch = inv.add("owner-bucket", "2026-10/tcp-q7m4/_archive/20261001-000000/old.md", b"old\n", project=A,
                   md5_only=True)
    lab_in = inv.add("lab-bucket", "tcp-q7m4/in/brief.md", b"brief\n", project=A, md5_only=True)
    report = inv.add("owner-bucket", "reports/projects.csv", b"codes\n", md5_only=True)
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    for rid, size in ((out, "6"), (arch, "4"), (lab_in, "6")):
        assert (got[rid]["action"], got[rid]["target"], got[rid]["size"]) == ("keep", "", size)
        assert got[rid]["reason"] == "in its layout place"
    assert (got[report]["action"], got[report]["reason"]) == ("keep", "reports/, not a project folder")


def test_bk2_move_a_loose_object_of_one_project_into_in_or_out(planted, tmp_path):
    inv = Inv()
    top = inv.add("owner-bucket", "offer.pdf", b"deliverable\n", md5_only=True)
    inv.add("host", "deliverables/offer.pdf", b"deliverable\n", project=A)
    loose = inv.add("owner-bucket", "2026-09/tcp-b3np/misc/notes.md", b"notes\n", project=B, md5_only=True)
    other = inv.add("owner-bucket", "old/brief.md", b"brief\n", md5_only=True)
    inv.add("host", "input/brief.md", b"brief\n", project=A)
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert (got[top]["action"], got[top]["target"], got[top]["project"], got[top]["size"]) == (
        "move", "2026-10/tcp-q7m4/out/offer.pdf", A, "12")
    assert got[top]["reason"] == "project by an identical copy, identical to a deliverable"
    assert (got[loose]["action"], got[loose]["target"]) == ("move", "2026-09/tcp-b3np/in/notes.md")
    assert got[loose]["reason"] == "project by its folder, no deliverable is identical"
    assert (got[other]["action"], got[other]["target"]) == ("move", "2026-10/tcp-q7m4/in/brief.md")


def test_bk2_a_loose_object_without_a_project_is_unsorted_and_never_deleted(planted, tmp_path):
    inv = Inv()
    lone = inv.add("owner-bucket", "misc/scan.pdf", b"scan\n", md5_only=True)
    both = inv.add("owner-bucket", "both.md", b"both\n", md5_only=True)
    inv.add("host", "input/both.md", b"both\n", project=A)
    inv.add("host", "input/both.md", b"both\n", project=B)
    nomonth = inv.add("owner-bucket", "x.md", b"x\n", project="tcp-zz99", md5_only=True)
    twice = inv.add("owner-bucket", "misc/scan-copy.pdf", b"scan\n", md5_only=True)
    sorted_once = inv.add("owner-bucket", "_unsorted/20261001-000000/misc/y.pdf", b"y\n", md5_only=True)
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert (got[lone]["action"], got[lone]["target"], got[lone]["size"], got[lone]["reason"]) == (
        "unsorted", "_unsorted/%s/misc/scan.pdf" % WHEN, "5", "no project")
    assert (got[both]["action"], got[both]["reason"]) == ("unsorted", "identical to copies of more than one project")
    assert (got[nomonth]["action"], got[nomonth]["reason"]) == ("unsorted", "no month for the project")
    assert got[twice]["action"] == "unsorted", "an identical copy outside one place is no delete"
    assert (got[sorted_once]["action"], got[sorted_once]["target"]) == ("keep", ""), "never _unsorted twice"
    assert not [r for r in rows if r["action"] == "delete"]


def test_bk2_archive_a_fetched_put_of_the_lab_bucket(planted, tmp_path):
    inv = Inv()
    put = lab_put(inv)
    inv.add("lab-bucket", INDEX_KEY, b"index\n", project=A, md5_only=True)
    asked = []

    def index(code):
        asked.append(code)
        return [index_row(PUT, "2026-09-29")]

    _, rows = make_plan(planted, inv, tmp_path, index=index)
    got = by_id(rows)[put]
    assert asked == [A]
    assert (got["action"], got["target"], got["size"], got["reason"]) == (
        "archive", "tcp-q7m4/_archive/2026-09/from-session/2026-09-20/doc/tcp-q7m4-doc-en-1010.md", "3",
        "fetched 30 days or more ago")
    assert filesplan.ARCHIVE_DAYS == 30


def test_bk2_archive_an_older_version_in_out_of_the_owner_bucket(planted, tmp_path):
    inv = Inv()
    old = inv.add("owner-bucket", "2026-10/tcp-q7m4/out/offer-v1.md", b"one\n", "2026-10-02T08:00:00Z", A,
                  md5_only=True)
    new = inv.add("owner-bucket", "2026-10/tcp-q7m4/out/offer-v2.md", b"two\n", "2026-10-09T08:00:00Z", A,
                  md5_only=True)
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert (got[old]["action"], got[old]["target"], got[old]["keeper"], got[old]["size"], got[old]["reason"]) == (
        "archive", "2026-10/tcp-q7m4/_archive/%s/offer-v1.md" % WHEN, new, "4", "older version")
    assert got[new]["action"] == "keep"


def test_bk2_delete_an_identical_copy_inside_one_place_and_a_stale_in_object(planted, tmp_path):
    inv = Inv()
    first = inv.add("owner-bucket", "2026-10/tcp-q7m4/out/r.md", b"report\n", "2026-10-02T08:00:00Z", A,
                    md5_only=True)
    copy = inv.add("owner-bucket", "2026-10/tcp-q7m4/out/r-copy.md", b"report\n", "2026-10-01T08:00:00Z", A,
                   md5_only=True)
    stale = inv.add("owner-bucket", "2026-10/tcp-q7m4/in/brief.txt", b"brief\n", project=A, md5_only=True)
    fresh = inv.add("owner-bucket", "2026-10/tcp-q7m4/in/new.txt", b"new\n", project=A, md5_only=True)
    again = inv.add("owner-bucket", "2026-10/tcp-q7m4/in/again.txt", b"put again\n", project=A, md5_only=True)
    bucket._remember(planted, "2026-10/tcp-q7m4/in/brief.txt", '"%s"' % md5(b"brief\n"))
    bucket._remember(planted, "2026-10/tcp-q7m4/in/again.txt", '"%s"' % md5(b"the first one\n"))
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    assert (got[copy]["action"], got[copy]["keeper"], got[copy]["size"], got[copy]["reason"]) == (
        "delete", first, "7", "identical")
    assert (got[stale]["action"], got[stale]["keeper"], got[stale]["size"], got[stale]["reason"]) == (
        "delete", "", "6", "stale-in: the import took it")
    assert (got[fresh]["action"], got[fresh]["reason"]) == ("keep", "in/, not taken by an import yet")
    assert got[again]["action"] == "keep", "a file put again under the same key is not the one taken"
    assert all(r["target"] == "" for r in rows if r["action"] == "delete")


def test_bk2_withheld_stays_withheld_and_a_path_that_is_no_key_too(planted, tmp_path):
    inv = Inv()
    hidden = inv.add("owner-bucket", inventory.WITHHELD, b"named\n", project=A, md5_only=True)
    lab_hidden = inv.add("lab-bucket", inventory.WITHHELD, b"named\n", project=A)
    odd = inv.add("owner-bucket", "misc//../x.md", b"odd\n", md5_only=True)
    _, rows = make_plan(planted, inv, tmp_path)
    got = by_id(rows)
    for rid in (hidden, lab_hidden, odd):
        assert (got[rid]["action"], got[rid]["target"], got[rid]["path"]) == ("withheld", "", inventory.WITHHELD)
    assert got[hidden]["size"] == "6" and got[odd]["reason"] == "path withheld"


# --------------------------------------------------------------------------- the rules around them


def test_bk2_a_fetched_put_29_days_old_stays_30_days_old_is_archive_never_fetched_stays(planted, tmp_path):
    inv = Inv()
    keys = {age: "tcp-q7m4/from-session/2026-01-0%d/doc/tcp-q7m4-doc-en-1010.md" % n
            for n, age in enumerate((29, 30, 31, None), start=1)}
    ids = {age: lab_put(inv, key, key.encode()) for age, key in keys.items()}
    unlisted = lab_put(inv, "tcp-q7m4/from-session/2026-01-09/doc/tcp-q7m4-doc-en-1010.md", b"u")
    inv.add("lab-bucket", INDEX_KEY, b"index\n", project=A, md5_only=True)
    marks = {29: "2026-09-30", 30: "2026-09-29", 31: "2026-09-28", None: "no"}
    _, rows = make_plan(planted, inv, tmp_path, index=lambda code: [index_row(keys[a], m) for a, m in marks.items()])
    got = by_id(rows)
    assert (got[ids[29]]["action"], got[ids[29]]["reason"]) == ("keep", "fetched less than 30 days ago")
    assert got[ids[30]]["action"] == "archive" and got[ids[31]]["action"] == "archive"
    assert (got[ids[None]]["action"], got[ids[None]]["reason"]) == ("keep", "not fetched"), "at any age"
    assert (got[unlisted]["action"], got[unlisted]["reason"]) == ("keep", "no row in INDEX.md")


def test_bk2_an_index_that_cannot_be_read_keeps_every_put_and_is_said(planted, tmp_path):
    inv = Inv()
    put = lab_put(inv)
    inv.add("lab-bucket", INDEX_KEY, b"index\n", project=A, md5_only=True)
    res, rows = make_plan(planted, inv, tmp_path, index=lambda code: None)
    assert (by_id(rows)[put]["action"], by_id(rows)[put]["reason"]) == ("keep", "INDEX.md not read")
    assert "INDEX.md of tcp-q7m4 not read: its puts stay keep" in res["notes"]


def test_bk2_without_an_index_in_the_inventory_nothing_is_read_and_the_puts_stay(planted, tmp_path):
    inv = Inv()
    put = lab_put(inv)
    called = []
    _, rows = make_plan(planted, inv, tmp_path, index=called.append)
    assert called == [] and (by_id(rows)[put]["action"], by_id(rows)[put]["reason"]) == ("keep", "no INDEX.md")


def test_bk2_an_old_put_hex_key_is_keep(planted, tmp_path):
    inv = Inv()
    old = lab_put(inv, "tcp-q7m4/from-session/2026-01-02/put-0123456789ab.md")
    inv.add("lab-bucket", INDEX_KEY, b"index\n", project=A, md5_only=True)
    _, rows = make_plan(planted, inv, tmp_path, index=lambda code: [])
    got = by_id(rows)[old]
    assert (got["action"], got["target"], got["reason"], got["size"]) == ("keep", "", "old key", "3")


def _mixed(planted, tmp_path):
    inv = Inv()
    inv.add("owner-bucket", "2026-10/tcp-q7m4/out/r.md", b"report\n", "2026-10-02T08:00:00Z", A, md5_only=True)
    inv.add("owner-bucket", "2026-10/tcp-q7m4/out/r-copy.md", b"report\n", "2026-10-01T08:00:00Z", A, md5_only=True)
    inv.add("owner-bucket", "2026-10/tcp-q7m4/in/brief.txt", b"brief\n", project=A, md5_only=True)
    inv.add("owner-bucket", "2026-10/tcp-q7m4/out/offer-v1.md", b"one\n", "2026-10-02T08:00:00Z", A, md5_only=True)
    inv.add("owner-bucket", "2026-10/tcp-q7m4/out/offer-v2.md", b"two\n", "2026-10-09T08:00:00Z", A, md5_only=True)
    inv.add("owner-bucket", "misc/scan.pdf", b"scan\n", md5_only=True)
    inv.add("owner-bucket", "misc/scan-copy.pdf", b"scan\n", md5_only=True)
    inv.add("owner-bucket", "offer.pdf", b"deliverable\n", md5_only=True)
    inv.add("host", "deliverables/offer.pdf", b"deliverable\n", project=A)
    inv.add("owner-bucket", inventory.WITHHELD, b"named\n", project=B, md5_only=True)
    inv.add("lab-bucket", "tcp-q7m4/from-session/2026-10-01/r.md", b"r\n", project=A, md5_only=True)
    inv.add("lab-bucket", "tcp-q7m4/from-session/2026-10-02/r.md", b"r\n", project=A, md5_only=True)
    lab_put(inv)
    inv.add("lab-bucket", INDEX_KEY, b"index\n", project=A, md5_only=True)
    inv.add("laptop", "notes.md", b"notes\n", project=A)
    return make_plan(planted, inv, tmp_path, index=lambda code: [index_row(PUT, "2026-08-01")],
                     taken={("2026-10/tcp-q7m4/in/brief.txt", md5(b"brief\n"))})


def test_bk2_a_delete_row_names_its_keeper_or_is_stale_in_and_unsorted_is_never_a_delete(planted, tmp_path):
    res, rows = _mixed(planted, tmp_path)
    deletes = [r for r in rows if r["action"] == "delete"]
    assert len(deletes) == 3
    ids = {r["id"] for r in rows}
    for r in deletes:
        assert (r["keeper"] in ids and r["keeper"] != r["id"]) or (
            r["keeper"] == "" and r["reason"].startswith("stale-in")), r
    assert {r["action"] for r in rows if r["place"] in filesplan.BUCKETS} == set(filesplan.BUCKET_ACTIONS)
    unsorted = [r for r in rows if r["action"] == "unsorted"]
    assert len(unsorted) == 2 and not {r["id"] for r in unsorted} & {r["id"] for r in deletes}


def test_bk2_targets_are_built_from_codes_months_stamps_and_key_parts_only(planted, tmp_path):
    _, rows = _mixed(planted, tmp_path)
    shapes = (r"^20[0-9]{2}-[0-9]{2}/tcp-[a-z0-9]{4}/(in|out)/[^/]+$",
              r"^_unsorted/%s/(.+)$" % WHEN,
              r"^tcp-[a-z0-9]{4}/_archive/20[0-9]{2}-[0-9]{2}/from-session/.+$",
              r"^20[0-9]{2}-[0-9]{2}/tcp-[a-z0-9]{4}/_archive/%s/[^/]+$" % WHEN)
    for r in rows:
        if r["place"] not in filesplan.BUCKETS:
            continue
        if r["action"] in ("move", "unsorted", "archive"):
            assert any(re.match(s, r["target"]) for s in shapes), r
            assert r["target"].rsplit("/", 1)[-1] == r["path"].rsplit("/", 1)[-1], "the name is the key's own"
        else:
            assert r["target"] == "", r
        if r["action"] == "unsorted":
            assert r["target"] == "_unsorted/%s/%s" % (WHEN, r["path"])


def test_bk2_plan_tsv_keeps_the_old_columns_in_order_and_adds_size_last(planted, tmp_path):
    res, rows = _mixed(planted, tmp_path)
    plan_file = res["folder"] / "PLAN.tsv"
    head = plan_file.read_text(encoding="utf-8").splitlines()[0].split("\t")
    assert head == ["id", "place", "action", "target", "project", "path", "keeper", "reason", "size"]
    assert all(r["size"].isdigit() for r in rows if r["place"] in filesplan.BUCKETS)
    assert all(r["size"] == "" for r in rows if r["place"] not in filesplan.BUCKETS)
    laptop = cleanfiles.read_plan(plan_file)
    assert [r["id"] for r in laptop] == [r["id"] for r in rows if r["place"] == "laptop"]
    assert laptop[0]["action"] == "move" and laptop[0]["target"] == "2026-10/tcp-q7m4/private/"


def test_bk2_the_laptop_reads_by_name_and_refuses_a_plan_without_a_column_it_needs(tmp_path):
    f = tmp_path / "p.tsv"
    f.write_text("size\treason\tkeeper\tpath\tproject\ttarget\taction\tplace\tid\n"
                 "9\tr\t\tp\ttcp-q7m4\t2026-10/tcp-q7m4/private/\tmove\tlaptop\tL-20261029-170000-0001\n",
                 encoding="utf-8")
    got = cleanfiles.read_plan(f)
    assert [(r["id"], r["action"], r["target"]) for r in got] == [
        ("L-20261029-170000-0001", "move", "2026-10/tcp-q7m4/private/")]
    f.write_text("id\tplace\taction\tproject\tpath\n", encoding="utf-8")
    try:
        cleanfiles.read_plan(f)
    except cleanfiles.FilesError as err:
        assert "target" in str(err)
    else:
        raise AssertionError("a plan without the column target is refused")


def _table(md: str, heading: str) -> list[list[str]]:
    part = md.split(heading, 1)[1].split("\n## ", 1)[0]
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in part.splitlines()
            if ln.startswith("| ") and not ln.startswith("| bucket")]


def test_bk2_plan_md_shows_counts_and_sizes_per_project_and_bucket_and_the_totals_add_up(planted, tmp_path):
    res, rows = _mixed(planted, tmp_path)
    md = (res["folder"] / "PLAN.md").read_text(encoding="utf-8")
    bucket_rows = [r for r in rows if r["place"] in filesplan.BUCKETS]
    per_project = _table(md, "## Bucket sizes per project")
    per_bucket = _table(md, "## Bucket sizes per bucket")
    for table in (per_project, per_bucket):
        assert sum(int(t[-2]) for t in table) == len(bucket_rows)
        assert sum(int(t[-1]) for t in table) == sum(int(r["size"]) for r in bucket_rows)
    assert ["owner-bucket", A, "delete", "2", "13"] in per_project
    assert ["lab-bucket", "delete", "1", "2"] in per_bucket
    deletes = [r for r in bucket_rows if r["action"] == "delete"]
    last = md.rstrip("\n").splitlines()[-1]
    assert last == "Total: %d bucket rows, %d bytes; delete %d rows, %d bytes." % (
        len(bucket_rows), sum(int(r["size"]) for r in bucket_rows), len(deletes),
        sum(int(r["size"]) for r in deletes))
    assert res["buckets"]["delete"] == {"rows": 3, "bytes": 15}


def test_bk2_the_laptop_rows_are_unchanged(planted, tmp_path):
    def laptop_rows(with_buckets: bool, folder):
        inv = Inv()
        inv.add("laptop", "kunde/notes.md", b"notes\n", project=A)
        inv.add("laptop", "copy/notes.md", b"notes\n", "2026-10-19T08:00:00Z", A)
        if with_buckets:
            inv.add("owner-bucket", "misc/scan.pdf", b"scan\n", md5_only=True)
            inv.add("owner-bucket", "2026-10/tcp-q7m4/out/r.md", b"report\n", project=A, md5_only=True)
            inv.add("lab-bucket", "tcp-q7m4/in/brief.md", b"brief\n", project=A, md5_only=True)
        folder.mkdir()
        _, rows = make_plan(planted, inv, folder)
        return [r for r in rows if r["place"] == "laptop"]

    before = laptop_rows(False, tmp_path / "a")
    shutil.rmtree(planted.shared / "files")
    after = laptop_rows(True, tmp_path / "b")
    assert before == after
    move = before[0]
    assert {k: move[k] for k in filesplan.COLUMNS[2:]} == {
        "action": "move", "target": "2026-10/tcp-q7m4/private/", "project": A, "path": "kunde/notes.md",
        "keeper": "", "reason": "private", "size": ""}


def test_bk2_the_run_line_has_the_bucket_counts_and_the_delete_size_and_moves_nothing(planted, tmp_path, capsys):
    inv = Inv()
    inv.add("laptop", "a.md", b"a\n", project=A)
    inv.add("owner-bucket", "2026-10/tcp-q7m4/out/r.md", b"report\n", "2026-10-02T08:00:00Z", A, md5_only=True)
    inv.add("owner-bucket", "2026-10/tcp-q7m4/out/r-copy.md", b"report\n", "2026-10-01T08:00:00Z", A, md5_only=True)
    inv.add("owner-bucket", "misc/scan.pdf", b"scan\n", md5_only=True)
    lap = inv.write(tmp_path / "laptop-20261029-170000.tsv", "laptop")
    own = inv.write(tmp_path / "owner-bucket-20261029-170000.tsv", "owner-bucket")
    before = own.read_text(encoding="utf-8")
    code = cli.main(["files", "plan", "--laptop", str(lap), "--buckets", str(own)])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "buckets 1 keep, 0 move, 1 unsorted, 0 archive, 1 delete, 0 withheld (delete 7 bytes)" in out
    assert "nothing moved, nothing deleted" in out
    assert own.read_text(encoding="utf-8") == before
