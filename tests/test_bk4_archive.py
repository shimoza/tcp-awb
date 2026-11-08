"""Task BK4: the archive rule where it arises, on `awb fetch` (the op `obs` ARCHIVE of the key service) and on `awb
bucket put` into out/ (the admin op `bucket_archive`), without a plan (D-B1, D-B3).

A stand-in key service (keys.Service on short socket paths, in this process) over two in-memory buckets whose etags
are the md5 of their bytes and whose copy can fail or change the bytes: no call leaves the process, no real key
service and no real bucket. The clock of the key service is fixed. Codes and the invented names of
tests/fixtures.py only.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re

import pytest

from awb import obs
from awb.tcp import keys, keys_exchange
from tests import fixtures as fx
from tests.test_bk1_put_keys import MemBucket, put, puts, rows, write
from tests.test_xchg import AK, SK, cust_project, quiet, run, short_dir  # noqa: F401 (fixtures)

NOW = datetime.datetime(2026, 10, 1, 9, 15, tzinfo=datetime.timezone.utc)
DAY = datetime.timedelta(days=1)
NOTES = "# Notes\n\nThe sizing of the two clusters and the load balancer.\n"


class Mem(MemBucket):
    """MemBucket with the md5 as etag, folders for a listing with "/", and three faults: a copy of a key in
    `fail_copy` raises, one in `grow` gets a byte more, one in `flip` gets its first byte changed. A key in
    `multipart` answers a multipart etag (no md5)."""

    def __init__(self, name: str):
        super().__init__(name)
        self.fail_copy: set[str] = set()
        self.grow: set[str] = set()
        self.flip: set[str] = set()
        self.multipart: set[str] = set()

    def _etag(self, key: str) -> str:
        md5 = hashlib.md5(self.objects[key]).hexdigest()
        return md5[:30] + "-2" if key in self.multipart else md5

    def list(self, prefix: str = "", delimiter: str = "", page: int = 1000) -> obs.Listing:
        found, folders = [], set()
        for k in sorted(self.objects):
            if not k.startswith(prefix):
                continue
            rest = k[len(prefix):]
            if delimiter and delimiter in rest:
                folders.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
            else:
                found.append(k)
        return obs.Listing([(k, len(self.objects[k]), self._etag(k)) for k in found], sorted(folders),
                           {k: "2026-10-01T09:00:00.000Z" for k in found})

    def head(self, key: str) -> dict | None:
        if key not in self.objects:
            return None
        return {"Content-Length": str(len(self.objects[key])), "ETag": '"%s"' % self._etag(key)}

    def copy(self, source: str, target: str) -> None:
        if source in self.fail_copy:
            raise obs.OBSError("HTTP 500", 500)
        data = self.objects[source]
        if source in self.grow:
            data += b"x"
        if source in self.flip:
            data = bytes([data[0] ^ 1]) + data[1:]
        self.objects[target] = data


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": NOW}
    monkeypatch.setattr(keys_exchange, "_now", lambda: now["t"])
    return now


@pytest.fixture
def lab():
    return Mem("awb-lab-eu-de")


@pytest.fixture
def own():
    return Mem("awb")


@pytest.fixture
def svc(home, lab, own, clock, tmp_path, monkeypatch):
    monkeypatch.setattr(keys.Service, "_obs_client",
                        lambda self, bucket, keys_: {lab.bucket: lab, own.bucket: own}[bucket])
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log")
        s._notify = lambda subject, message: None
        s.start()
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(s.admin_path))
        keys.unlock(s.admin_path, ["test-1/lab/ak", "test-1/lab/sk"], reader={"test-1/lab/ak": AK,
                    "test-1/lab/sk": SK}.get, settings={"bucket_tenant": "test-1", "lab_bucket": lab.bucket,
                                                         "owner_bucket": own.bucket, "max_mb": "1"})
        try:
            yield s
        finally:
            s.stop()


# --------------------------------------------------------------------------- awb fetch


def fetch(code: str, into, capsys, *more) -> str:
    rc, out, err = run(["fetch", code, "--into", str(into)] + list(more), capsys)
    assert rc == 0, err
    fx.assert_no_fixture_name(out + err, "the output of awb fetch")
    return out


def two_fetched(project, lab, clock, capsys, into) -> tuple[str, str]:
    """A Markdown put fetched on NOW and a csv put fetched one day later; the clock stands at NOW + 30 days."""
    c = project.code
    put([str(write(project, "notes.md", NOTES)), "--kind", "offer"], capsys)
    fetch(c, into, capsys)
    clock["t"] = NOW + DAY
    put([str(write(project, "sizes.csv", "a,b\n")), "--kind", "sizing"], capsys)
    fetch(c, into, capsys)
    old = "%s/from-session/2026-10-01/offer/%s-offer-en-0915.md" % (c, c)
    new = "%s/from-session/2026-10-02/sizing/%s-sizing-en-0915.csv" % (c, c)
    assert [r[2] for r in rows(lab, c)] == [new, old] and [r[-1] for r in rows(lab, c)] == ["2026-10-02",
                                                                                           "2026-10-01"]
    clock["t"] = NOW + 30 * DAY
    return old, new


def test_bk4_fetch_archives_a_put_fetched_30_days_ago_and_keeps_one_fetched_29_days_ago(svc, lab, cust_project,
                                                                                        capsys, clock, tmp_path):
    c = cust_project.code
    old, new = two_fetched(cust_project, lab, clock, capsys, tmp_path / "f")
    body, page = lab.objects[old], lab.objects[old[:-3] + ".html"]
    out = fetch(c, tmp_path / "f", capsys)
    assert "archived 1 put(s) fetched 30 days or more ago" in out
    target = "%s/_archive/2026-10/from-session/2026-10-01/offer/%s-offer-en-0915.md" % (c, c)
    assert lab.objects[target] == body and old not in lab.objects
    assert lab.objects[target[:-3] + ".html"] == page and old[:-3] + ".html" not in lab.objects, "the page goes along"
    assert puts(lab, c) == [new], "the put fetched 29 days ago stays"
    # INDEX.md drops the row of the moved put and keeps the other as it was
    assert rows(lab, c) == [["2026-10-02", "09:15", new, "sizing", "en", "4", "none", "2026-10-02"]]
    out = fetch(c, tmp_path / "f", capsys)
    assert "archived 0 put(s)" in out and lab.objects[target] == body


def test_bk4_no_archive_keeps_both(svc, lab, cust_project, capsys, clock, tmp_path):
    c = cust_project.code
    old, new = two_fetched(cust_project, lab, clock, capsys, tmp_path / "f")
    out = fetch(c, tmp_path / "f", capsys, "--no-archive")
    assert "archived" not in out
    assert old in lab.objects and new in lab.objects and not [k for k in lab.objects if "/_archive/" in k]
    assert [r[2] for r in rows(lab, c)] == [new, old]
    assert "archived 1 put(s)" in fetch(c, tmp_path / "f", capsys), "without the flag the rule runs"


@pytest.mark.parametrize("fault", ["copy", "size", "md5"])
def test_bk4_a_failed_copy_or_a_size_or_md5_mismatch_leaves_the_source_and_is_counted(svc, lab, cust_project, capsys,
                                                                                     clock, tmp_path, fault):
    c = cust_project.code
    old, new = two_fetched(cust_project, lab, clock, capsys, tmp_path / "f")
    target = keys_exchange.archive_key(old)
    if fault == "copy":
        lab.fail_copy.add(old)
    elif fault == "size":
        lab.grow.add(old)
        lab.multipart.add(target)           # no md5 to compare: the size alone must catch it
    else:
        lab.flip.add(old)                   # the same size: the md5 alone must catch it
    body = lab.objects[old]
    out = fetch(c, tmp_path / "f", capsys)
    assert "archived 0 put(s) fetched 30 days or more ago, 1 move(s) failed" in out
    assert lab.objects[old] == body, "the source stays"
    assert [r[2] for r in rows(lab, c)] == [new, old], "its row stays in INDEX.md"
    assert old[:-3] + ".html" in lab.objects, "the page stays with its put"


def test_bk4_the_archive_op_is_the_owners_alone(svc, lab, cust_project, capsys, clock, tmp_path):
    c = cust_project.code
    two_fetched(cust_project, lab, clock, capsys, tmp_path / "f")
    before = dict(lab.objects)
    answer = svc._call_socket({"op": "obs", "method": "ARCHIVE", "project": c}, os.getuid() + 4711)
    answer = answer[0] if isinstance(answer, tuple) else answer
    assert not answer["ok"] and lab.objects == before
    a = keys.request(svc.call_path, {"op": "obs", "method": "ARCHIVE", "project": c})
    assert a["ok"] and a["archived"] == 1 and lab.objects != before
    # the owner bucket's move is on the admin socket, which takes the owner's uid alone
    key = "2026-10/%s/out/offer v1.md" % c
    own_req = {"op": "bucket_archive", "key": key, "target": "2026-10/%s/_archive/20261031-091500/offer v1.md" % c}
    assert svc._admin(own_req, os.getuid() + 4711) == {"ok": False, "error": "refused"}


# --------------------------------------------------------------------------- awb bucket put


def bucket_put(code: str, f, capsys, *more) -> tuple[int, str, str]:
    rc, out, err = run(["bucket", "put", code, str(f)] + list(more), capsys)
    fx.assert_no_fixture_name(out + err, "the output of awb bucket put")
    return rc, out, err


def out_of(own, base: str) -> list[str]:
    return sorted(k[len(base + "out/"):] for k in own.objects
                  if k.startswith(base + "out/") and k != base + "out/" and not k.endswith(".keep"))


def archived(own, base: str) -> list[str]:
    return sorted(k[len(base):] for k in own.objects if k.startswith(base + "_archive/"))


def test_bk4_bucket_put_moves_the_older_version_of_the_group_to_archive_and_leaves_another_group(svc, own,
                                                                                                cust_project,
                                                                                                capsys, tmp_path):
    c = cust_project.code
    base = "%s/%s/" % (cust_project.created[:7], c)
    first = tmp_path / "offer v1.md"
    first.write_text(NOTES, encoding="utf-8")
    other = tmp_path / "offer.csv"
    other.write_text("a,b\n", encoding="utf-8")
    sheet = tmp_path / "sizes.csv"
    sheet.write_text("c,d\n", encoding="utf-8")
    for f in (first, other, sheet):
        assert bucket_put(c, f, capsys)[0] == 0
    old_md, old_page = own.objects[base + "out/offer v1.md"], own.objects[base + "out/offer v1.html"]
    second = tmp_path / "offer v2.md"
    second.write_text(NOTES + "\nOne more line.\n", encoding="utf-8")
    rc, out, err = bucket_put(c, second, capsys)
    assert rc == 0, err
    assert out_of(own, base) == ["offer v2.html", "offer v2.md", "offer.csv", "sizes.csv"]
    moved = archived(own, base)
    assert len(moved) == 2 and all(re.fullmatch(r"_archive/\d{8}-\d{6}/offer v1\.(md|html)", k) for k in moved)
    assert own.objects[base + moved[1]] == old_md and own.objects[base + moved[0]] == old_page
    assert own.objects[base + "out/offer v2.md"] == second.read_bytes()
    assert out.count("archived to") == 2 and out.splitlines()[-1].startswith("get it:")


def test_bk4_an_identical_file_is_not_put_again(svc, own, cust_project, capsys, tmp_path):
    c = cust_project.code
    base = "%s/%s/" % (cust_project.created[:7], c)
    f = tmp_path / "offer v2.md"
    f.write_text(NOTES, encoding="utf-8")
    assert bucket_put(c, f, capsys)[0] == 0
    same = tmp_path / "offer final.md"              # the same group, the same bytes
    same.write_bytes(f.read_bytes())
    rc, out, err = bucket_put(c, same, capsys)
    assert rc == 0, err
    assert "%sout/offer v2.md is the same file (size and md5): not put again" % base in out
    assert out_of(own, base) == ["offer v2.html", "offer v2.md"] and archived(own, base) == []
    rc, out, _ = bucket_put(c, f, capsys, "--replace")
    assert rc == 0 and "not put again" in out


def test_bk4_replace_keeps_its_meaning_for_the_same_name(svc, own, cust_project, capsys, tmp_path):
    c = cust_project.code
    base = "%s/%s/" % (cust_project.created[:7], c)
    f = tmp_path / "offer v2.md"
    f.write_text(NOTES, encoding="utf-8")
    assert bucket_put(c, f, capsys)[0] == 0
    f.write_text(NOTES + "\nChanged.\n", encoding="utf-8")
    rc, _, err = bucket_put(c, f, capsys)
    assert rc == 2 and "--replace" in err
    assert bucket_put(c, f, capsys, "--replace")[0] == 0
    assert own.objects[base + "out/offer v2.md"] == f.read_bytes() and archived(own, base) == []


def test_bk4_a_failed_archive_in_out_leaves_the_older_version_and_puts_the_new_one(svc, own, cust_project, capsys,
                                                                                  tmp_path):
    c = cust_project.code
    base = "%s/%s/" % (cust_project.created[:7], c)
    first = tmp_path / "sizes v1.csv"
    first.write_text("a,b\n", encoding="utf-8")
    assert bucket_put(c, first, capsys)[0] == 0
    own.flip.add(base + "out/sizes v1.csv")
    second = tmp_path / "sizes v2.csv"
    second.write_text("a,b,c\n", encoding="utf-8")
    rc, out, err = bucket_put(c, second, capsys)
    assert rc == 0 and "not archived, it stays in out/" in err and "md5" in err
    assert own.objects[base + "out/sizes v1.csv"] == first.read_bytes()
    assert own.objects[base + "out/sizes v2.csv"] == second.read_bytes()
