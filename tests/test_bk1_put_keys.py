"""Task BK1: readable put keys, kind folders and INDEX.md (awb/tcp/keys_exchange.py, awb/tcp/xchg.py, awb/tcp/fetch.py).

A stand-in key service (keys.Service on short socket paths, in this process) over an in-memory lab bucket
(`MemBucket`): no call leaves the process, no real key service and no real bucket. The clock of the key service is
fixed. Every name is an invented name of tests/fixtures.py.

Four tests here replace tests of the old put-<hex> key (tests_replaced of B-041):
test_xchg.py::test_a_put_lands_under_its_id_and_tells_the_owner_by_the_id_alone,
test_dn_notes_html.py::test_xchg_put_of_a_markdown_file_writes_the_html_beside_it_and_names_both,
test_dn_notes_html.py::test_xchg_put_of_another_file_writes_no_page and
test_gt_put_no_gate.py::test_gt_a_session_put_of_a_customer_deliverable_succeeds_and_replaces_the_secret.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path

import pytest

from awb import obs
from awb.tcp import keys, keys_exchange, xchg
from tests import fixtures as fx
from tests.test_dn_notes_html import OFFER
from tests.test_gt_put_no_gate import secret_value, with_secret
from tests.test_xchg import AK, SK, cust_project, lab_project, quiet, run, short_dir  # noqa: F401 (fixtures)

NOW = datetime.datetime(2026, 11, 6, 14, 32, 10, tzinfo=datetime.timezone.utc)
DAY = NOW.date().isoformat()
ENGLISH = "The image of the lab was imported in four minutes and the test of the load balancer passed.\n"
GERMAN = "die migration der zwei cluster ist fertig und der test ist mit dem load balancer nicht fehlgeschlagen.\n"


class MemBucket:
    """A bucket in memory with the calls of obs.Client the exchange uses."""

    def __init__(self, name: str):
        self.bucket = name
        self.objects: dict[str, bytes] = {}

    def list(self, prefix: str = "", delimiter: str = "", page: int = 1000) -> obs.Listing:
        found = sorted(k for k in self.objects if k.startswith(prefix))
        return obs.Listing([(k, len(self.objects[k]), "etag-%d" % i) for i, k in enumerate(found)], [],
                           {k: "2026-11-06T14:00:00.000Z" for k in found})

    def head(self, key: str) -> dict | None:
        if key not in self.objects:
            return None
        return {"Content-Length": str(len(self.objects[key])), "ETag": '"etag"'}

    def get(self, key: str, to: Path, if_match: str | None = None) -> Path:
        if key not in self.objects:
            raise obs.OBSError("HTTP 404", 404)
        Path(to).write_bytes(self.objects[key])
        return Path(to)

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.objects[key] = bytes(data)

    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> None:
        self.objects[key] = Path(path).read_bytes()

    def copy(self, source: str, target: str) -> None:
        self.objects[target] = self.objects[source]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": NOW}
    monkeypatch.setattr(keys_exchange, "_now", lambda: now["t"])
    return now


@pytest.fixture
def lab():
    return MemBucket("awb-lab-eu-de")


@pytest.fixture
def svc(home, lab, clock, tmp_path, monkeypatch):
    own = MemBucket("awb")
    monkeypatch.setattr(keys.Service, "_obs_client",
                        lambda self, bucket, keys_: {lab.bucket: lab, own.bucket: own}[bucket])
    notes: list = []
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log")
        s._notify = lambda subject, message: notes.append((subject, message))
        s.start()
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        keys.unlock(s.admin_path, ["test-1/lab/ak", "test-1/lab/sk"], reader={"test-1/lab/ak": AK,
                    "test-1/lab/sk": SK}.get, settings={"bucket_tenant": "test-1", "lab_bucket": lab.bucket,
                                                         "owner_bucket": own.bucket, "max_mb": "1"})
        s.notes = notes
        try:
            yield s
        finally:
            s.stop()


@pytest.fixture
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


def write(project, name: str, text: str = ENGLISH) -> Path:
    f = Path(project.path) / "evidence" / name
    f.write_text(text, encoding="utf-8")
    return f


def puts(lab, code: str) -> list[str]:
    """The put objects under from-session/, INDEX.md counted apart."""
    return sorted(k for k in lab.objects if k.startswith("%s/from-session/" % code) and k != index(code))


def index(code: str) -> str:
    return "%s/from-session/INDEX.md" % code


def rows(lab, code: str) -> list[list[str]]:
    text = lab.objects[index(code)].decode("utf-8")
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in text.splitlines()
            if line.startswith("| 20")]


def put(argv, capsys):
    code, out, err = run(["xchg", "put"] + argv, capsys)
    assert code == 0, err
    return out, err


# --------------------------------------------------------------------------- the key of a put


def test_bk1_a_put_without_kind_and_lang_gets_doc_the_guessed_language_and_the_minute(svc, lab, lab_project,
                                                                                        capsys):
    c = lab_project.code
    put([str(write(lab_project, "result.csv", "flavor,count\ns3.large.2,4\n"))], capsys)
    put([str(write(lab_project, "notes.txt", GERMAN))], capsys)
    assert puts(lab, c) == ["%s/from-session/%s/doc/%s-doc-de-1432.txt" % (c, DAY, c),
                            "%s/from-session/%s/doc/%s-doc-en-1432.csv" % (c, DAY, c)]


def test_bk1_kind_and_lang_name_the_folder_and_the_key(svc, lab, lab_project, capsys, clock):
    c = lab_project.code
    clock["t"] = NOW.replace(hour=9, minute=5)
    out, _ = put([str(write(lab_project, "sizes.csv", "a,b\n")), "--kind", "Sizing", "--lang", "de"], capsys)
    key = "%s/from-session/%s/sizing/%s-sizing-de-0905.csv" % (c, DAY, c)
    assert puts(lab, c) == [key] and ("as %s (" % key) in out
    put([str(write(lab_project, "dump.backup1", "x\n")), "--kind", "dump"], capsys)
    assert "%s/from-session/%s/dump/%s-dump-en-0905" % (c, DAY, c) in lab.objects, "no extension that is not plain"
    for bad in (["--kind", "x"], ["--kind", "two words"], ["--lang", "deu"]):
        code, _, err = run(["xchg", "put", str(write(lab_project, "x.csv", "a\n"))] + bad, capsys)
        assert code == 1 and ("--kind" in err or "--lang" in err)


def test_bk1_a_second_put_in_the_same_minute_gets_2_and_never_overwrites(svc, lab, lab_project, capsys, clock):
    c = lab_project.code
    base = "%s/from-session/%s/doc/%s-doc-en-1432" % (c, DAY, c)
    for n in range(3):
        put([str(write(lab_project, "r%d.csv" % n, "row,%d\n" % n))], capsys)
    assert [lab.objects[k] for k in (base + ".csv", base + "-2.csv", base + "-3.csv")] == \
        [b"row,0\n", b"row,1\n", b"row,2\n"]
    put([str(write(lab_project, "page.md", "# Page\n\nThe first text.\n"))], capsys)
    put([str(write(lab_project, "page.md", "# Page\n\nThe second text.\n"))], capsys)
    assert b"first" in lab.objects[base + ".md"] and b"second" in lab.objects[base + "-2.md"]
    assert b"first" in lab.objects[base + ".html"] and b"second" in lab.objects[base + "-2.html"]
    put([str(write(lab_project, "page.html", "<p>third</p>\n"))], capsys)
    assert lab.objects[base + "-3.html"] == b"<p>third</p>\n", "a page beside a put is never overwritten"
    clock["t"] = NOW.replace(minute=40)
    base = base[:-4] + "1440"
    put([str(write(lab_project, "plain.html", "<p>plain</p>\n"))], capsys)
    put([str(write(lab_project, "after.md", "# After\n\nThe md after the page.\n"))], capsys)
    assert lab.objects[base + ".html"] == b"<p>plain</p>\n", "the page of an md never overwrites an html put"
    assert b"after the page" in lab.objects[base + "-2.md"] and b"after the page" in lab.objects[base + "-2.html"]


def test_bk1_as_with_a_clean_name_gives_its_stem(svc, lab, lab_project, capsys):
    c = lab_project.code
    f = write(lab_project, "r.md")
    out, _ = put([str(f), "--as", "sizing notes (v2).md", "--kind", "sizing"], capsys)
    key = "%s/from-session/%s/sizing/%s-sizing-sizing-notes-v2.md" % (c, DAY, c)
    assert key in lab.objects and key[:-3] + ".html" in lab.objects and "not used" not in out
    put([str(f), "--as", "a" * 30 + "_" + "b" * 30 + ".md"], capsys)
    stem = ("a" * 30 + "-" + "b" * 9)
    assert "%s/from-session/%s/doc/%s-doc-%s.md" % (c, DAY, c, stem) in lab.objects, "at most 40 characters"


def test_bk1_as_with_a_registered_name_gives_the_neutral_key_and_a_line_without_the_name(svc, lab, lab_project,
                                                                                          capsys, clock):
    c = lab_project.code
    out, err = put([str(write(lab_project, "r.md")), "--as", "offer %s.md" % fx.CUSTOMER_FORMS[0], "--kind",
                    "offer"], capsys)
    key = "%s/from-session/%s/offer/%s-offer-en-1432.md" % (c, DAY, c)
    assert puts(lab, c) == [key[:-3] + ".html", key]
    assert xchg.AS_NOT_USED in out
    line = next(x for x in out.splitlines() if "not used" in x)
    fx.assert_no_fixture_name(line, "the line of the name not used")
    fx.assert_no_fixture_name("\n".join(lab.objects) + err + "".join(s + m for s, m in svc.notes), "keys and mail")
    out, _ = put([str(write(lab_project, "r.md")), "--as", "notes %s.md" % fx.PERSON_FORMS[1]], capsys)
    assert "%s/from-session/%s/doc/%s-doc-en-1432.md" % (c, DAY, c) in lab.objects and xchg.AS_NOT_USED in out
    fx.assert_no_fixture_name(out + "\n".join(lab.objects), "the second put")
    # the name itself is checked: its stem (qv-rtz-notes) would pass the check of the key and still carry it
    clock["t"] = NOW.replace(minute=33)
    out, _ = put([str(write(lab_project, "r.csv", "a\n")), "--as", "%s notes.csv" % fx.ORG_FORMS[1]], capsys)
    assert "%s/from-session/%s/doc/%s-doc-en-1433.csv" % (c, DAY, c) in lab.objects and xchg.AS_NOT_USED in out
    assert not [k for k in lab.objects if "rtz" in k]


def test_bk1_a_kind_word_that_is_a_registered_name_falls_back_to_doc(svc, lab, lab_project, capsys):
    c = lab_project.code
    word = fx.CUSTOMER_FORMS[1].lower()
    out, _ = put([str(write(lab_project, "r.csv", "a\n")), "--kind", word], capsys)
    assert puts(lab, c) == ["%s/from-session/%s/doc/%s-doc-en-1432.csv" % (c, DAY, c)]
    assert xchg.KIND_NOT_USED in out
    fx.assert_no_fixture_name(out + "\n".join(lab.objects) + lab.objects[index(c)].decode(), "the put of the kind")


def test_bk1_a_session_never_chooses_a_readable_key(svc, lab, lab_project, tmp_path):
    c = lab_project.code
    up = tmp_path / "x.csv"
    up.write_text("a\n", encoding="utf-8")
    key = "%s/from-session/%s/doc/%s-doc-en-0000.csv" % (c, DAY, c)
    a = keys.request(svc.call_path, {"op": "obs", "method": "PUT", "project": c, "key": key}, upload=up)
    assert not a["ok"] and not lab.objects
    a = keys.request(svc.call_path, {"op": "obs", "method": "PUT", "project": c, "kind": "two words"}, upload=up)
    assert not a["ok"] and not lab.objects
    a = keys.request(svc.call_path, {"op": "obs", "method": "PUT", "project": c, "name": "x.csv"}, upload=up)
    assert a["ok"] and a["key"] == "%s/from-session/%s/doc/%s-doc-en-1432.csv" % (c, DAY, c)


# --------------------------------------------------------------------------- put_key_ok


@pytest.mark.parametrize("key", [
    "tcp-q7m4/from-session/2026-11-06/doc/tcp-q7m4-doc-en-1432.md",
    "tcp-q7m4/from-session/2026-11-06/offer/tcp-q7m4-offer-de-0905-2.md",
    "tcp-q7m4/from-session/2026-11-06/sizing/tcp-q7m4-sizing-sizing-notes-v2.xlsx",
    "tcp-q7m4/from-session/2026-11-06/dump/tcp-q7m4-dump-en-0905",
    "tcp-q7m4/from-session/2026-10-14/put-0123456789ab.md",
    "tcp-q7m4/from-session/2026-10-14/put-0123456789ab",
])
def test_bk1_put_key_ok_takes_the_new_and_the_old_form(key):
    assert xchg.put_key_ok(key, "tcp-q7m4")


@pytest.mark.parametrize("key", [
    "tcp-q7m4/from-session/2026-11-06/tcp-q7m4-doc-en-1432.md",              # no kind folder
    "tcp-q7m4/from-session/doc/tcp-q7m4-doc-en-1432.md",                     # no date folder
    "tcp-q7m4/from-session/2026-11-06/doc/x/tcp-q7m4-doc-en-1432.md",        # a level deeper
    "tcp-q7m4/in/2026-11-06/doc/tcp-q7m4-doc-en-1432.md",                    # outside from-session/
    "tcp-q7m4/from-session/INDEX.md",
    "tcp-q7m4/from-session/2026-11-06/offer/tcp-q7m4-doc-en-1432.md",        # the folder is not the file's kind
    "tcp-q7m4/from-session/2026-11-06/doc/tcp-q7m4-offer-en-1432.md",
    "tcp-q7m4/from-session/2026-11-06/../doc/tcp-q7m4-doc-en-1432.md",
    "tcp-q7m4/from-session/2026-11-06/doc/../tcp-q7m4-doc-en-1432.md",
    "tcp-q7m4/from-session/2026-11-06/../put-0123456789ab.md",
    "tcp-zzzz/from-session/2026-11-06/doc/tcp-zzzz-doc-en-1432.md",          # another project
    "tcp-q7m4/from-session/2026-11-06/doc/tcp-zzzz-doc-en-1432.md",
    "tcp-zzzz/from-session/2026-10-14/put-0123456789ab.md",
    "tcp-q7m4/from-session/2026-11-06/doc/tcp-q7m4-doc-EN-1432.md",          # upper case
    "tcp-q7m4/from-session/2026-11-06/doc/tcp-q7m4-doc-.md",
    "tcp-q7m4/from-session/2026-11-06/doc/tcp-q7m4-doc-en--1432.md",
    "tcp-q7m4/from-session/2026-11-06/doc/tcp-q7m4-doc-en-1432.backup1",
    "tcp-q7m4/from-session/2026-11-06/doc/tcp-q7m4-doc-%s.md" % ("a" * 44),
    "tcp-q7m4/from-session/2026-11-06/doc/tcp-q7m4-doc-en-1432.md\n",
    "tcp-q7m4/from-session/2026-11-06/put-0123456789ab.md\n",
])
def test_bk1_put_key_ok_refuses_every_other_key(key):
    assert not xchg.put_key_ok(key, "tcp-q7m4")


# --------------------------------------------------------------------------- INDEX.md


def test_bk1_index_after_two_puts_and_one_fetch(svc, lab, cust_project, capsys, clock, tmp_path, owner_terminal):
    c = cust_project.code
    named = write(cust_project, "offer %s.md" % fx.CUSTOMER_FORMS[0], "# Offer\n\nThe offer for %s.\n" % fx.CUSTOMER_CODE)
    put([str(named), "--kind", "offer"], capsys)
    first = "%s/from-session/%s/offer/%s-offer-en-1432.md" % (c, DAY, c)
    into = tmp_path / "fetched"
    code, out, err = run(["fetch", c, "--into", str(into)], capsys)
    assert code == 0 and "2 new file(s)" in out, err                  # the md and its page
    assert (into / ("%s-offer-en-1432.md" % c)).is_file(), "fetch writes the file under its readable stem"
    clock["t"] = NOW.replace(hour=15, minute=7)
    put([str(write(cust_project, "sizes.csv", "a,b\n")), "--kind", "sizing", "--lang", "de"], capsys)
    second = "%s/from-session/%s/sizing/%s-sizing-de-1507.csv" % (c, DAY, c)
    assert rows(lab, c) == [[DAY, "15:07", second, "sizing", "de", "4", "none", "no"],
                            [DAY, "14:32", first, "offer", "en", str(len(lab.objects[first])), "none", DAY]]
    text = lab.objects[index(c)].decode("utf-8")
    assert "| date | time | key | kind | lang | size | review | fetched |" in text
    fx.assert_no_fixture_name(text, "INDEX.md")
    assert "offer for" not in text.lower(), "no title of the text"


def test_bk1_a_fetched_mark_is_the_owners_alone(svc, lab, lab_project, capsys):
    c = lab_project.code
    put([str(write(lab_project, "r.csv", "a\n"))], capsys)
    key = puts(lab, c)[0]
    before = dict(lab.objects)
    answer = svc._call_socket({"op": "obs", "method": "FETCHED", "key": key, "project": c}, os.getuid() + 4711)
    answer = answer[0] if isinstance(answer, tuple) else answer
    assert not answer["ok"] and lab.objects == before
    a = keys.request(svc.call_path, {"op": "obs", "method": "FETCHED", "key": index(c), "project": c})
    assert not a["ok"] and lab.objects == before
    a = keys.request(svc.call_path, {"op": "obs", "method": "FETCHED", "key": key, "project": c})
    assert a["ok"] and rows(lab, c)[0][-1] == DAY


def test_bk1_clean_drops_the_deleted_rows_and_the_last_one_takes_index_md(svc, lab, lab_project, capsys, clock):
    c = lab_project.code
    clock["t"] = NOW - datetime.timedelta(days=2)
    put([str(write(lab_project, "old.csv", "a\n"))], capsys)
    clock["t"] = NOW
    put([str(write(lab_project, "new.csv", "b\n"))], capsys)
    old, new = puts(lab, c)
    assert [r[2] for r in rows(lab, c)] == [new, old]
    code, out, err = run(["xchg", "clean", c, "--dry-run"], capsys)
    assert code == 0 and out.count("would delete") == 2 and "INDEX.md" not in out, err
    code, out, err = run(["xchg", "clean", c, "--older-than", DAY, "--go"], capsys)
    assert code == 0 and old not in lab.objects and new in lab.objects, err
    assert [r[2] for r in rows(lab, c)] == [new]
    code, out, err = run(["xchg", "clean", c, "--go"], capsys)
    assert code == 0 and "1 object(s) deleted" in out, err
    assert not [k for k in lab.objects if k.startswith(c + "/")], "INDEX.md goes with its last row"
    a = keys.request(svc.call_path, {"op": "obs", "method": "DELETE", "key": index(c), "project": c})
    assert not a["ok"]


def test_bk1_fetch_list_and_clean_still_work_on_an_old_put_key(svc, lab, cust_project, capsys, tmp_path,
                                                                 owner_terminal):
    c = cust_project.code
    old = "%s/from-session/2026-10-14/put-0123456789ab.md" % c
    lab.objects[old] = ("The offer for %s.\n" % fx.CUSTOMER_CODE).encode()
    put([str(write(cust_project, "r.csv", "a\n")), "--kind", "sizing"], capsys)
    code, out, _ = run(["xchg", "list"], capsys)
    lines = out.splitlines()
    assert lines[0].split() == ["sizing", "%s/sizing/%s-sizing-en-1432.csv" % (DAY, c), "2", "bytes"]
    assert lines[1].split()[:2] == ["-", "2026-10-14/put-0123456789ab.md"] and len(lines) == 2
    into = tmp_path / "fetched"
    code, out, err = run(["fetch", c, "--into", str(into)], capsys)
    assert code == 0 and "2 new file(s)" in out and "not marked" not in out, err
    assert fx.CUSTOMER_FORMS[0] in (into / "put-0123456789ab.md").read_text(encoding="utf-8")
    assert rows(lab, c)[0][-1] == DAY and len(rows(lab, c)) == 1
    code, out, err = run(["xchg", "clean", c, "--older-than", DAY, "--go"], capsys)
    assert code == 0 and old not in lab.objects and "1 object(s) deleted" in out, err
    assert len(rows(lab, c)) == 1


# --------------------------------------------------------------------------- the replaced tests


def test_bk1_a_put_lands_under_its_readable_key_and_tells_the_owner_by_the_checked_key_alone(svc, lab, lab_project,
                                                                                               capsys):
    """Replaces test_xchg.py::test_a_put_lands_under_its_id_and_tells_the_owner_by_the_id_alone: the key is the
    readable key the service built, and the mail names the file by it alone, never by a name the session chose; the
    session's own output still shows its file name."""
    c = lab_project.code
    out, _ = put([str(write(lab_project, "result.md"))], capsys)
    key = "%s/from-session/%s/doc/%s-doc-en-1432.md" % (c, DAY, c)
    assert key in lab.objects and "result.md" in out
    ident = key.rsplit("/", 1)[-1]
    subject, message = svc.notes[-1]
    assert c in subject and ident in message and "result" not in subject + message
    assert "from-session/" not in message and "awb-lab" not in message
    code, out, _ = run(["xchg", "list"], capsys)
    assert ident in out


def test_bk1_a_put_of_a_markdown_file_writes_the_html_beside_it_and_names_both(svc, lab, lab_project, capsys):
    """Replaces test_dn_notes_html.py::test_xchg_put_of_a_markdown_file_writes_the_html_beside_it_and_names_both:
    two put objects with one stem, INDEX.md counted apart."""
    c = lab_project.code
    f = write(lab_project, "%s-offer-en.md" % c, OFFER)
    out, _ = put([str(f), "--kind", "offer"], capsys)
    out = out.splitlines()
    sent = puts(lab, c)
    assert len(sent) == 2 and all(xchg.put_key_ok(k, c) for k in sent) and len(lab.objects) == 3
    page, md = sent
    assert page.endswith(".html") and md.endswith(".md") and page[:-5] == md[:-3]
    assert md == "%s/from-session/%s/offer/%s-offer-en-1432.md" % (c, DAY, c)
    assert lab.objects[md] == f.read_bytes()
    html = lab.objects[page].decode("utf-8")
    assert "<th>Flavor</th>" in html and "<li>first point" in html and "<title>Offer for the lab</title>" in html
    assert out[0].startswith("put: %s-offer-en.md as %s (" % (c, md))
    assert out[1] == "put: the same text as HTML as %s (open it, select all, paste into the mail)" % page
    assert out[2] == xchg.get_it_line("awb-lab-eu-de", md) and len(out) == 3
    assert ("folder %s/ " % md.rsplit("/", 1)[0]) in out[2]
    assert len(svc.notes) == 1 and md.rsplit("/", 1)[-1] in svc.notes[-1][1], "one mail, by the key of the md"
    assert [r[2] for r in rows(lab, c)] == [md], "one row: the page is not a put of its own"


def test_bk1_a_put_of_another_file_writes_no_page(svc, lab, lab_project, capsys):
    """Replaces test_dn_notes_html.py::test_xchg_put_of_another_file_writes_no_page, INDEX.md counted apart."""
    c = lab_project.code
    out, _ = put([str(write(lab_project, "sizes.csv", "flavor,count\ns3.large.2,4\n"))], capsys)
    sent = puts(lab, c)
    assert len(sent) == 1 and sent[0].endswith(".csv") and sorted(lab.objects) == sorted(sent + [index(c)])
    out = out.splitlines()
    assert len(out) == 2 and out[1] == xchg.get_it_line("awb-lab-eu-de", sent[0])


def test_bk1_a_session_put_of_a_customer_deliverable_succeeds_and_replaces_the_secret(svc, lab, cust_project,
                                                                                       capsys):
    """Replaces test_gt_put_no_gate.py::test_gt_a_session_put_of_a_customer_deliverable_succeeds_and_replaces_the_
    secret: the md and its HTML without the secret, INDEX.md counted apart and free of the secret and of the name."""
    value = secret_value()
    d = Path(cust_project.path) / "deliverables" / ("plan %s.md" % fx.CUSTOMER_FORMS[0])
    d.write_text(with_secret(value) + "For %s.\n" % fx.CUSTOMER_FORMS[0], encoding="utf-8")
    code, out, err = run(["xchg", "put", str(d)], capsys)
    assert code == 0, err
    assert "review: none; 1 secret replaced" in err.splitlines()
    sent = [lab.objects[k] for k in puts(lab, cust_project.code)]
    assert len(sent) == 2 and all(value.encode() not in v and b"[secret]" in v for v in sent)   # md and its HTML
    text = lab.objects[index(cust_project.code)].decode("utf-8")
    assert value not in text
    subject, message = svc.notes[-1]
    fx.assert_no_fixture_name(subject + message + text + "\n".join(lab.objects), "the put mail, INDEX.md and keys")
