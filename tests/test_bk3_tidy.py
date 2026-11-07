"""Task BK3 of presentations/buckets: `awb files tidy --go PLAN` carries out the bucket rows of a move plan of `awb
files plan`. Plans written in the test into the temporary Workbench of the `home` fixture, an in-memory stand-in for
each bucket (never a real bucket, no loopback), the word yes injected (no terminal). Codes only."""
from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from awb import bucket_layout, cli, config, filesplan, filestidy, inventory, obs

A = "tcp-q7m4"
WHEN = "20261107-090000"
NOW = dt.datetime(2026, 11, 7, 10, 0, tzinfo=dt.timezone.utc)
PUT = "tcp-q7m4/from-session/2026-09-20/doc/tcp-q7m4-doc-en-1010.md"
PUT_ARCHIVE = "tcp-q7m4/_archive/2026-09/from-session/2026-09-20/doc/tcp-q7m4-doc-en-1010.md"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class Mem:
    """One bucket in memory with the calls of obs.Client that the tidy uses: head, copy, delete and put_file. A key in
    `multipart` answers a multipart etag; `mangle` changes the bytes of a copy; `fail` holds (call, key) that fail."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.multipart: set[str] = set()
        self.mangle = None
        self.fail: set[tuple[str, str]] = set()
        self.calls: list[tuple[str, str]] = []

    def _call(self, name: str, key: str) -> None:
        self.calls.append((name, key))
        if (name, key) in self.fail:
            raise obs.OBSError("HTTP 500", 500)

    def head(self, key: str) -> dict | None:
        self._call("head", key)
        if key not in self.objects:
            return None
        data = self.objects[key]
        etag = md5(data)[:30] + "-2" if key in self.multipart else md5(data)
        return {"Content-Length": str(len(data)), "ETag": '"%s"' % etag}

    def copy(self, source: str, target: str) -> None:
        self._call("copy", source)
        data = self.objects[source]
        self.objects[target] = self.mangle(data) if self.mangle else data

    def delete(self, key: str) -> None:
        self._call("delete", key)
        self.objects.pop(key, None)

    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> None:
        self._call("put", key)
        self.objects[key] = Path(path).read_bytes()

    def changes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] in ("copy", "delete", "put")]


@pytest.fixture(autouse=True)
def own_terminal(monkeypatch):
    for name in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)


class Plan:
    """Bucket rows of an invented plan, numbered per place as the inventories number them."""

    def __init__(self):
        self.rows: list[dict] = []
        self.count: dict[str, int] = {}

    def add(self, place: str, action: str, path: str, data: bytes, target: str = "", keeper: str = "",
            reason: str = "", project: str = A) -> str:
        self.count[place] = self.count.get(place, 0) + 1
        rid = inventory.row_id(place, "20261107-080000", self.count[place])
        self.rows.append({"id": rid, "place": place, "action": action, "target": target, "project": project,
                          "path": path, "keeper": keeper, "reason": reason, "size": str(len(data))})
        return rid

    def write(self, home: config.Paths, stamp: str = WHEN) -> Path:
        folder = home.shared / "files" / ("plan-%s" % stamp)
        folder.mkdir(parents=True)
        (folder / "PLAN.tsv").write_text(filesplan.tsv(self.rows), encoding="utf-8")
        (folder / "PLAN.md").write_text("# Move plan\n", encoding="utf-8")
        return folder


def go(home, folder, own: Mem, lab: Mem | None = None, *, now=NOW, word="yes", taken=frozenset(), **kw):
    said: list[str] = []
    code = filestidy.tidy(home, folder, clients={"owner-bucket": own, "lab-bucket": lab or Mem()},
                          confirm=lambda prompt: word, now=now, taken=taken, say=said.append, **kw)
    return code, said


def log_of(home, now=NOW) -> dict[str, dict]:
    path = home.shared / "files" / ("tidy-%s" % inventory.stamp(now)) / "LOG.tsv"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].split("\t") == list(filestidy.LOG_COLUMNS)
    return {r["id"]: r for r in (dict(zip(filestidy.LOG_COLUMNS, ln.split("\t"))) for ln in lines[1:])}


# --------------------------------------------------------------------------- each action


def test_bk3_each_action_on_a_planted_row(home):
    loose, scan, old, new = b"deliverable\n", b"scan\n", b"offer v1\n", b"offer v2\n"
    copy, brief, put, big = b"same offer\n", b"brief\n", b"put\n", b"multipart bytes\n"
    own = Mem({"offer.pdf": loose, "misc/scan.pdf": scan, "2026-10/tcp-q7m4/out/offer-v1.md": old,
               "2026-10/tcp-q7m4/out/offer-v2.md": new, "2026-10/tcp-q7m4/out/final.pdf": copy,
               "2026-10/tcp-q7m4/out/final (2).pdf": copy, "2026-10/tcp-q7m4/in/brief.md": brief,
               "2026-10/tcp-q7m4/out/kept.md": b"kept\n", "old/large.zip": big})
    own.multipart.add("old/large.zip")
    lab = Mem({PUT: put})
    plan = Plan()
    move = plan.add("owner-bucket", "move", "offer.pdf", loose, "2026-10/tcp-q7m4/out/offer.pdf")
    unsorted = plan.add("owner-bucket", "unsorted", "misc/scan.pdf", scan, "_unsorted/%s/misc/scan.pdf" % WHEN,
                        project="none")
    newest = plan.add("owner-bucket", "keep", "2026-10/tcp-q7m4/out/offer-v2.md", new, reason="in its layout place")
    archive = plan.add("owner-bucket", "archive", "2026-10/tcp-q7m4/out/offer-v1.md", old,
                       "2026-10/tcp-q7m4/_archive/%s/offer-v1.md" % WHEN, newest, "older version")
    keeper = plan.add("owner-bucket", "keep", "2026-10/tcp-q7m4/out/final.pdf", copy, reason="in its layout place")
    delete = plan.add("owner-bucket", "delete", "2026-10/tcp-q7m4/out/final (2).pdf", copy, keeper=keeper,
                      reason="identical")
    stale = plan.add("owner-bucket", "delete", "2026-10/tcp-q7m4/in/brief.md", brief,
                     reason="stale-in: the import took it")
    keep = plan.add("owner-bucket", "keep", "2026-10/tcp-q7m4/out/kept.md", b"kept\n", reason="in its layout place")
    withheld = plan.add("owner-bucket", "withheld", "[withheld]", b"secret name\n", reason="path withheld")
    multi = plan.add("owner-bucket", "move", "old/large.zip", big, "2026-10/tcp-q7m4/in/large.zip")
    archived = plan.add("lab-bucket", "archive", PUT, put, PUT_ARCHIVE, reason="fetched 30 days or more ago")
    code, said = go(home, plan.write(home), own, lab, taken={("2026-10/tcp-q7m4/in/brief.md", md5(brief))})
    assert code == 0, said
    assert own.objects.get("2026-10/tcp-q7m4/out/offer.pdf") == loose and "offer.pdf" not in own.objects
    assert own.objects.get("_unsorted/%s/misc/scan.pdf" % WHEN) == scan and "misc/scan.pdf" not in own.objects
    assert own.objects.get("2026-10/tcp-q7m4/_archive/%s/offer-v1.md" % WHEN) == old
    assert "2026-10/tcp-q7m4/out/offer-v1.md" not in own.objects
    assert "2026-10/tcp-q7m4/out/final (2).pdf" not in own.objects and own.objects["2026-10/tcp-q7m4/out/final.pdf"]
    assert "2026-10/tcp-q7m4/in/brief.md" not in own.objects
    assert own.objects["2026-10/tcp-q7m4/out/kept.md"] == b"kept\n"
    assert own.objects["2026-10/tcp-q7m4/in/large.zip"] == big and "old/large.zip" not in own.objects
    assert lab.objects == {PUT_ARCHIVE: put}
    log = log_of(home)
    for rid in (move, unsorted, archive, delete, stale, multi, archived):
        assert log[rid]["result"] == "ok", log[rid]
    assert (log[move]["action"], log[move]["source"], log[move]["target"]) == (
        "move", "offer.pdf", "2026-10/tcp-q7m4/out/offer.pdf")
    assert log[move]["reason"] == "copied, size and md5 compared, source deleted"
    assert log[multi]["reason"] == "copied, size compared only (multipart etag), source deleted"
    assert log[delete]["reason"] == "the keeper %s has the same md5, deleted" % keeper
    assert log[stale]["reason"] == "stale-in: the import took it, deleted"
    assert (log[archived]["source"], log[archived]["target"]) == (PUT, PUT_ARCHIVE)
    for rid in (keep, withheld, newest, keeper):
        assert rid not in log
    touched = {k for _, k in own.calls}
    assert "2026-10/tcp-q7m4/out/kept.md" not in touched and "[withheld]" not in touched


def test_bk3_a_withheld_path_or_a_target_out_of_shape_is_never_acted_on(home):
    own = Mem({"misc/a.md": b"a\n", "2026-10/tcp-q7m4/out/b.md": b"b\n"})
    plan = Plan()
    held = plan.add("owner-bucket", "move", "[withheld]", b"x\n", "2026-10/tcp-q7m4/in/x.md")
    wrong = plan.add("owner-bucket", "move", "misc/a.md", b"a\n", "reports/a.md")
    inside = plan.add("owner-bucket", "unsorted", "2026-10/tcp-q7m4/out/b.md", b"b\n",
                      "_unsorted/%s/2026-10/tcp-q7m4/out/b.md" % WHEN)
    code, _ = go(home, plan.write(home), own)
    log = log_of(home)
    assert code == 0 and log[held]["result"] == "skipped" and log[held]["reason"] == "path withheld, never acted on"
    for rid in (wrong, inside):
        assert log[rid]["result"] == "skipped" and "shape" in log[rid]["reason"]
    assert {k: v for k, v in own.objects.items() if not k.startswith("reports/")} == {
        "misc/a.md": b"a\n", "2026-10/tcp-q7m4/out/b.md": b"b\n"}
    assert [c for c in own.changes() if c[0] != "put"] == []


# --------------------------------------------------------------------------- the checks


def test_bk3_a_copy_that_comes_back_different_leaves_the_source_in_place(home):
    data = b"deliverable\n"
    for n, (mangle, why) in enumerate(((lambda d: d + b"x", "size"), (lambda d: d[::-1], "md5"))):
        own = Mem({"offer.pdf": data})
        own.mangle = mangle
        plan = Plan()
        rid = plan.add("owner-bucket", "move", "offer.pdf", data, "2026-10/tcp-q7m4/out/offer.pdf")
        now = NOW + dt.timedelta(seconds=n)
        code, _ = go(home, plan.write(home, "20261107-09000%d" % n), own, now=now)
        log = log_of(home, now)
        assert code == 1 and log[rid]["result"] == "failed"
        assert log[rid]["reason"] == "the copy differs in %s; the source stays" % why
        assert own.objects["offer.pdf"] == data and ("delete", "offer.pdf") not in own.calls


def test_bk3_a_delete_without_keeper_is_refused_and_one_whose_keeper_is_gone_is_skipped(home):
    copy = b"same\n"
    own = Mem({"2026-10/tcp-q7m4/out/a (2).md": copy, "2026-10/tcp-q7m4/out/b (2).md": copy,
               "2026-10/tcp-q7m4/out/c (2).md": copy, "2026-10/tcp-q7m4/out/c.md": b"other\n"})
    plan = Plan()
    lone = plan.add("owner-bucket", "delete", "2026-10/tcp-q7m4/out/a (2).md", copy, reason="identical")
    gone_keeper = plan.add("owner-bucket", "keep", "2026-10/tcp-q7m4/out/b.md", copy)
    orphan = plan.add("owner-bucket", "delete", "2026-10/tcp-q7m4/out/b (2).md", copy, keeper=gone_keeper,
                      reason="identical")
    other_keeper = plan.add("owner-bucket", "keep", "2026-10/tcp-q7m4/out/c.md", b"other\n")
    unlike = plan.add("owner-bucket", "delete", "2026-10/tcp-q7m4/out/c (2).md", copy, keeper=other_keeper,
                      reason="identical")
    stale = plan.add("owner-bucket", "delete", "2026-10/tcp-q7m4/in/d.md", b"d\n", reason="stale-in: taken")
    own.objects["2026-10/tcp-q7m4/in/d.md"] = b"d\n"
    code, said = go(home, plan.write(home), own, taken=set())
    log = log_of(home)
    assert code == 0
    assert log[lone]["result"] == "skipped" and log[lone]["reason"].startswith("refused")
    assert log[orphan]["result"] == "skipped" and log[orphan]["reason"] == "the keeper is gone"
    assert log[unlike]["result"] == "skipped" and log[unlike]["reason"].startswith("the keeper")
    assert log[stale]["result"] == "skipped", "a stale-in the vault's record does not hold stays"
    assert not [c for c in own.calls if c[0] == "delete"]
    assert "delete owner-bucket 2026-10/tcp-q7m4/out/a (2).md 5 bytes (keeper none: refused)" in said


def test_bk3_an_existing_target_is_never_overwritten_and_a_changed_object_is_skipped(home):
    own = Mem({"offer.pdf": b"new\n", "2026-10/tcp-q7m4/out/offer.pdf": b"there\n", "misc/grown.md": b"grown now\n"})
    plan = Plan()
    taken = plan.add("owner-bucket", "move", "offer.pdf", b"new\n", "2026-10/tcp-q7m4/out/offer.pdf")
    grown = plan.add("owner-bucket", "unsorted", "misc/grown.md", b"grown\n", "_unsorted/%s/misc/grown.md" % WHEN)
    code, _ = go(home, plan.write(home), own)
    log = log_of(home)
    assert code == 0
    assert log[taken]["result"] == "skipped" and "never overwritten" in log[taken]["reason"]
    assert own.objects["2026-10/tcp-q7m4/out/offer.pdf"] == b"there\n" and own.objects["offer.pdf"] == b"new\n"
    assert log[grown]["result"] == "skipped" and log[grown]["reason"].startswith("the object changed size")
    assert own.objects["misc/grown.md"] == b"grown now\n" and "_unsorted/%s/misc/grown.md" % WHEN not in own.objects
    assert [c for c in own.changes() if c[0] != "put"] == []


# --------------------------------------------------------------------------- the run


def test_bk3_a_plan_older_than_24_hours_is_refused(home):
    own = Mem({"offer.pdf": b"x\n"})
    plan = Plan()
    plan.add("owner-bucket", "move", "offer.pdf", b"x\n", "2026-10/tcp-q7m4/out/offer.pdf")
    folder = plan.write(home, "20261106-095959")
    with pytest.raises(filestidy.TidyError, match="older than 24 hours: make a new plan"):
        go(home, folder, own)
    assert own.calls == [] and own.objects == {"offer.pdf": b"x\n"}
    code, said = go(home, folder, own, now=dt.datetime(2026, 11, 7, 9, 59, 59, tzinfo=dt.timezone.utc))
    assert code == 0 and "2026-10/tcp-q7m4/out/offer.pdf" in own.objects, said


def test_bk3_dry_run_changes_nothing_and_no_yes_changes_nothing(home, capsys):
    own = Mem({"offer.pdf": b"x\n", "2026-10/tcp-q7m4/out/a.md": b"a\n", "2026-10/tcp-q7m4/out/b.md": b"a\n"})
    plan = Plan()
    plan.add("owner-bucket", "move", "offer.pdf", b"x\n", "2026-10/tcp-q7m4/out/offer.pdf")
    keeper = plan.add("owner-bucket", "keep", "2026-10/tcp-q7m4/out/a.md", b"a\n")
    plan.add("owner-bucket", "delete", "2026-10/tcp-q7m4/out/b.md", b"a\n", keeper=keeper, reason="identical")
    folder = plan.write(home)
    before = dict(own.objects)
    asked: list[str] = []
    said: list[str] = []
    code = filestidy.tidy(home, folder, clients={"owner-bucket": own, "lab-bucket": Mem()}, dry_run=True,
                          confirm=lambda p: asked.append(p) or "yes", now=NOW, taken=set(), say=said.append)
    assert code == 0 and asked == [] and own.calls == [] and own.objects == before
    assert any(ln.split() == ["owner-bucket", "move", "1", "row", "2", "bytes"] for ln in said), said
    assert "delete owner-bucket 2026-10/tcp-q7m4/out/b.md 2 bytes (keeper %s)" % keeper in said
    assert not list((home.shared / "files").glob("tidy-*"))
    for word in ("", "y", "YES please"):
        code, said = go(home, folder, own, word=word)
        assert code == 2 and own.calls == [] and own.objects == before and "no yes, nothing changed" in said[-1]
    assert not list((home.shared / "files").glob("tidy-*"))
    with pytest.raises(filestidy.TidyError, match="without a terminal"):
        filestidy.tidy(home, folder, clients={"owner-bucket": own}, now=NOW, taken=set(), say=said.append)
    assert own.calls == []
    assert cli.main(["files", "tidy", "--go", str(folder)]) == 2
    assert "without a terminal" in capsys.readouterr().err and own.objects == before


def test_bk3_the_work_user_and_an_assistant_session_are_refused(home, monkeypatch, capsys):
    own = Mem({"offer.pdf": b"x\n"})
    plan = Plan()
    plan.add("owner-bucket", "move", "offer.pdf", b"x\n", "2026-10/tcp-q7m4/out/offer.pdf")
    folder = plan.write(home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    with pytest.raises(filestidy.TidyError, match="own shell"):
        go(home, folder, own)
    assert cli.main(["files", "tidy", "--go", str(folder), "--dry-run"]) == 2
    assert "own shell" in capsys.readouterr().err
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(filestidy.TidyError, match="assistant session"):
        go(home, folder, own)
    assert own.calls == []


def test_bk3_a_struck_row_is_not_acted_on(home):
    own = Mem({"a.md": b"a\n", "b.md": b"b\n"})
    plan = Plan()
    kept = plan.add("owner-bucket", "move", "a.md", b"a\n", "2026-10/tcp-q7m4/in/a.md")
    struck = plan.add("owner-bucket", "move", "b.md", b"b\n", "2026-10/tcp-q7m4/in/b.md")
    folder = plan.write(home)
    tsv = folder / "PLAN.tsv"
    tsv.write_text("".join(ln for ln in tsv.read_text(encoding="utf-8").splitlines(True) if struck not in ln),
                   encoding="utf-8")
    code, _ = go(home, folder, own)
    log = log_of(home)
    assert code == 0 and list(log) == [kept]
    assert own.objects["b.md"] == b"b\n" and "2026-10/tcp-q7m4/in/b.md" not in own.objects
    assert not [k for _, k in own.calls if k.startswith("b.md") or k.endswith("/b.md")]


def test_bk3_one_failing_row_does_not_stop_the_others_and_gives_exit_1(home):
    own = Mem({"a.md": b"a\n", "b.md": b"b\n", "c.md": b"c\n"})
    own.fail.add(("copy", "a.md"))
    own.fail.add(("delete", "c.md"))
    plan = Plan()
    first = plan.add("owner-bucket", "move", "a.md", b"a\n", "2026-10/tcp-q7m4/in/a.md")
    second = plan.add("owner-bucket", "move", "b.md", b"b\n", "2026-10/tcp-q7m4/in/b.md")
    third = plan.add("owner-bucket", "move", "c.md", b"c\n", "2026-10/tcp-q7m4/in/c.md")
    code, said = go(home, plan.write(home), own)
    log = log_of(home)
    assert code == 1
    assert log[first]["result"] == "failed" and log[first]["reason"] == "the copy failed (HTTP 500); the source stays"
    assert log[second]["result"] == "ok" and own.objects["2026-10/tcp-q7m4/in/b.md"] == b"b\n"
    assert log[third]["result"] == "failed" and "the source could not be deleted" in log[third]["reason"]
    assert own.objects["a.md"] == b"a\n" and own.objects["c.md"] == b"c\n"
    assert "3 rows: 1 ok, 0 skipped, 2 failed" in said[-1]


def test_bk3_the_log_goes_to_reports_tidy_of_the_owner_bucket(home):
    own = Mem({"a.md": b"a\n", "b.md": b"b\n"})
    lab = Mem()
    plan = Plan()
    plan.add("owner-bucket", "move", "a.md", b"a\n", "2026-10/tcp-q7m4/in/a.md")
    code, said = go(home, plan.write(home), own, lab)
    stamp = inventory.stamp(NOW)
    key = "reports/tidy/%s/LOG.tsv" % stamp
    assert bucket_layout.tidy_key(stamp) == key
    local = home.shared / "files" / ("tidy-%s" % stamp) / "LOG.tsv"
    assert code == 0 and own.objects[key] == local.read_bytes() and ("put", key) in own.calls
    assert lab.calls == [] and said[-1].endswith("put %s" % key)
    own.fail.add(("put", bucket_layout.tidy_key(inventory.stamp(NOW + dt.timedelta(seconds=1)))))
    plan2 = Plan()
    plan2.add("owner-bucket", "move", "b.md", b"b\n", "2026-10/tcp-q7m4/in/b.md")
    code, said = go(home, plan2.write(home, "20261107-090001"), own, now=NOW + dt.timedelta(seconds=1))
    assert code == 1 and "the log did not go up" in said[-1]
