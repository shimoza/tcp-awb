"""Review of the key put and archive exchange (awb/tcp/keys_exchange.py): hostile buckets, objects that change
between the head and the get, temporary files and their descriptors, names that must never reach a page and one
failing row of an archive.

The functions of the key service are called directly with a Context over an in-memory bucket: no key service, no
real bucket, no network. Codes and the invented names of tests/fixtures.py only.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import obs
from awb.tcp import keys_exchange, xchg
from tests import fixtures as fx
from tests.test_bk1_put_keys import MemBucket

CODE = "tcp-q7m4"
NOW = datetime.datetime(2026, 11, 6, 14, 32, 10, tzinfo=datetime.timezone.utc)
MIB = 1024 * 1024
FAKE_SECRET = "pass" + "word" + "=" + "Hq7" + "zz91"


def context(home, c, active=(CODE,)):
    settings = dict(xchg.DEFAULTS, bucket_tenant="test-1", max_mb="1")
    return keys_exchange.Context(settings, lambda alias: object(), lambda bucket, keys_: c, lambda: home,
                                 lambda code: SimpleNamespace(code=code, customer="") if code in active else None,
                                 lambda subject, message: None)


def tmp_files(home) -> list[str]:
    d = home.vault / "tmp"
    return sorted(os.listdir(d)) if d.is_dir() else []


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(keys_exchange, "_now", lambda: NOW)


class Growing(MemBucket):
    """A bucket whose head names the stored size but whose get writes `extra` bytes more (the object changed)."""

    def __init__(self, extra: int):
        super().__init__("awb-lab-eu-de")
        self.extra = extra

    def get(self, key, to, if_match=None):
        Path(to).write_bytes(self.objects[key] + b"x" * self.extra)
        return Path(to)


def test_a_get_that_grows_past_the_limit_after_the_head_is_refused_and_leaves_no_file(home):
    c = Growing(2 * MIB)
    key = CODE + "/in/notes.txt"
    c.objects[key] = b"small\n"
    with pytest.raises(xchg.XchgError) as err:
        keys_exchange.serve_obs(context(home, c), {"method": "GET", "key": key, "project": CODE}, None)
    assert "limit" in str(err.value)
    assert tmp_files(home) == []


def test_a_failed_get_leaves_no_temporary_file_in_the_service_folder(home):
    class Failing(MemBucket):
        def get(self, key, to, if_match=None):
            Path(to).write_bytes(b"half")
            raise obs.OBSError("HTTP 500", 500)

    c = Failing("awb-lab-eu-de")
    key = CODE + "/in/notes.txt"
    c.objects[key] = b"whole\n"
    with pytest.raises(obs.OBSError):
        keys_exchange.serve_obs(context(home, c), {"method": "GET", "key": key, "project": CODE}, None)
    assert tmp_files(home) == []


def _recording_mkstemp(monkeypatch) -> list[tuple[int, int]]:
    """(descriptor, inode) of every file mkstemp makes for the key service."""
    made: list[tuple[int, int]] = []
    real = keys_exchange.tempfile.mkstemp

    def mkstemp(*args, **kwargs):
        fd, name = real(*args, **kwargs)
        made.append((fd, os.fstat(fd).st_ino))
        return fd, name

    monkeypatch.setattr(keys_exchange.tempfile, "mkstemp", mkstemp)
    return made


def _still_open(made: list[tuple[int, int]]) -> list[int]:
    out = []
    for fd, ino in made:
        try:
            if os.fstat(fd).st_ino == ino:
                out.append(fd)
        except OSError:
            pass
    return out


def test_a_get_and_a_take_close_the_descriptor_of_their_temporary_file(home, monkeypatch):
    made = _recording_mkstemp(monkeypatch)
    c = MemBucket("awb-lab-eu-de")
    key = CODE + "/in/notes.txt"
    c.objects[key] = b"The load balancer test passed.\n"
    ctx = context(home, c)
    try:
        _, got = keys_exchange.serve_obs(ctx, {"method": "GET", "key": key, "project": CODE}, None)
        assert got is not None and got.read_bytes() == c.objects[key]
        c.objects[xchg.INBOX + "sizing.txt"] = b"Two clusters and one load balancer.\n"
        found = keys_exchange._inbox_files(ctx, "lab")
        ident = keys_exchange._file_id("lab", found[0]["key"], found[0]["etag"])
        answer, out = keys_exchange.serve_take_lab(ctx, {"project": CODE, "id": ident})
        assert answer["state"] == "taken" and out is not None
        assert len(made) >= 2
        assert _still_open(made) == []
    finally:
        for fd in _still_open(made):
            os.close(fd)


def test_an_index_that_grows_past_its_limit_after_the_head_is_not_read(home, clock, monkeypatch):
    c = MemBucket("awb-lab-eu-de")
    key = "%s/from-session/2026-11-06/doc/%s-doc-en-1432.md" % (CODE, CODE)
    c.objects[key] = b"# Notes\n"
    keys_exchange.write_index(c, CODE, [{"date": "2026-11-06", "time": "14:32", "key": key, "kind": "doc",
                                          "lang": "en", "size": 8, "review": "none", "fetched": "no"}])
    stored = c.objects[keys_exchange.index_key(CODE)]
    assert len(keys_exchange.read_index(context(home, c), c, CODE)) == 1
    monkeypatch.setattr(keys_exchange, "INDEX_MAX", len(stored) + 10)

    class Grown(MemBucket):
        def get(self, k, to, if_match=None):
            Path(to).write_bytes(self.objects[k] + b"\n" * 4096)
            return Path(to)

    g = Grown("awb-lab-eu-de")
    g.objects = dict(c.objects)
    assert keys_exchange.read_index(context(home, g), g, CODE) == []
    assert tmp_files(home) == []


def test_an_index_whose_length_is_no_number_reads_as_none(home):
    class Odd(MemBucket):
        def head(self, k):
            return {"Content-Length": "12" + "abc", "ETag": '"etag"'} if k in self.objects else None

    c = Odd("awb-lab-eu-de")
    c.objects[keys_exchange.index_key(CODE)] = b"| x |\n"
    assert keys_exchange.read_index(context(home, c), c, CODE) == []


def test_the_page_of_a_put_takes_no_title_from_a_name_the_name_check_finds(home, clock, tmp_path):
    c = MemBucket("awb-lab-eu-de")
    body = tmp_path / "body"
    body.write_text("The sizing of the two clusters is done and the test of the load balancer passed.\n",
                    encoding="utf-8")
    name = fx.CUSTOMER_FORMS[1] + "-plan.md"
    answer, _ = keys_exchange.serve_obs(context(home, c), {"method": "PUT", "project": CODE, "name": name}, body)
    assert answer["ok"] and answer.get("page")
    page = c.objects[answer["page"]].decode("utf-8")
    # B-089: no name check of an object name: the title comes from the name as it stands
    assert fx.CUSTOMER_FORMS[1] in page


def test_a_plain_name_still_gives_the_title_of_the_page(home, clock, tmp_path):
    c = MemBucket("awb-lab-eu-de")
    body = tmp_path / "body"
    body.write_text("The sizing of the two clusters is done.\n", encoding="utf-8")
    answer, _ = keys_exchange.serve_obs(context(home, c), {"method": "PUT", "project": CODE, "name": "sizing.md"},
                                        body)
    assert "<title>sizing</title>" in c.objects[answer["page"]].decode("utf-8")


@pytest.mark.parametrize("bad", ["\n", "\r", "\x00", "\x1b", "\x7f", "\t"])
def test_a_key_with_a_control_character_is_outside_the_allowed_folders(bad):
    assert keys_exchange.allowed_key(CODE + "/in/notes.txt", "GET", CODE)
    assert not keys_exchange.allowed_key(CODE + "/in/no" + bad + "tes.txt", "GET", CODE)
    assert not keys_exchange.allowed_key(CODE + "/in/no" + bad + "tes.txt", "DELETE", CODE)
    assert not keys_exchange.allowed_key(CODE + "/from-session/x" + bad, "PUT", CODE)


def test_one_failing_row_of_an_archive_never_stops_the_others_and_echoes_no_error_text(home, clock):
    old = (NOW.date() - datetime.timedelta(days=40)).isoformat()
    first = "%s/from-session/2026-09-20/doc/%s-doc-en-0900.md" % (CODE, CODE)
    second = "%s/from-session/2026-09-21/doc/%s-doc-en-1000.md" % (CODE, CODE)

    class Flaky(MemBucket):
        def head(self, k):
            if k == first:
                raise obs.OBSError("HTTP 503 " + FAKE_SECRET, 503)
            return super().head(k)

    c = Flaky("awb-lab-eu-de")
    c.objects[first] = b"# One\n"
    c.objects[second] = b"# Two\n"
    rows = [{"date": d, "time": t, "key": k, "kind": "doc", "lang": "en", "size": 6, "review": "none",
             "fetched": old} for d, t, k in (("2026-09-20", "09:00", first), ("2026-09-21", "10:00", second))]
    keys_exchange.write_index(c, CODE, rows)
    answer = keys_exchange.archive_fetched(context(home, c), c, CODE)
    assert (answer["archived"], answer["failed"]) == (1, 1)
    assert FAKE_SECRET not in repr(answer) and "OBSError" in answer["failures"][0]
    assert second not in c.objects and keys_exchange.archive_key(second) in c.objects
    left = [r["key"] for r in keys_exchange.index_rows(c.objects[keys_exchange.index_key(CODE)].decode(), CODE)]
    assert left == [first]
