"""The pass of a local model in `awb clean --llm`, the local corrections of `awb clean teach` and `awb clean measure`
(B-051). The model is a fake on 127.0.0.1 in the test, never a real one. Invented names only (tests/fixtures.py):
the nickname below is a name the rules miss (calibration/redteam/EXPECTED.md, wipe-meetings, en-nickname)."""
from __future__ import annotations

import builtins
import io
import json
import os
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from awb import clean, cleanfetch, cleanlm, cli, laptop, obs
from tests import fixtures as fx

PROJECT = "tcp-q7m4"
NICK = fx.MEETING_NICKNAMES[1]
SHORT = "Who checks the backup job tonight?\n%s will do it, the restore test is on Monday.\n" % NICK
FILLER = "We check the backup job and the restore test of the night. " * 40
LONG = SHORT + FILLER + "\nThe quorbel script runs on Kubernetes in Berlin.\n"


class Fake:
    """An OpenAI-compatible chat endpoint on 127.0.0.1: `propose(piece)` gives the spans of a piece; `mode` slow,
    500 or nonjson makes it fail. Every request is recorded, also one a proxy would get (any method)."""

    def __init__(self, propose=None, mode="ok"):
        self.propose = propose or (lambda piece: [])
        self.mode = mode
        self.pieces: list[str] = []
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _any(self):
                outer.requests.append(self.command)
                data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    piece = json.loads(data)["messages"][-1]["content"]
                except (ValueError, KeyError, IndexError, TypeError):
                    piece = ""
                outer.pieces.append(piece)
                try:
                    if outer.mode == "slow":
                        time.sleep(3)
                    if outer.mode == "500":
                        self.send_error(500)
                        return
                    content = "no json here, sorry" if outer.mode == "nonjson" else json.dumps(outer.propose(piece))
                    answer = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})
                    raw = answer.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass                       # the client gave up (the timeout test)

            do_POST = do_GET = do_CONNECT = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.url = self.base + "/v1/chat/completions"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fakes():
    made: list[Fake] = []

    def make(propose=None, mode="ok"):
        f = Fake(propose, mode)
        made.append(f)
        return f

    yield make
    for f in made:
        f.close()


@pytest.fixture
def lap(tmp_path, monkeypatch):
    monkeypatch.setenv(laptop.HOME_ENV, str(tmp_path / "laptop"))
    for name in laptop.PUT_ENV + laptop.PUT_OPTIONAL_ENV + (cleanlm.URL_ENV, cleanlm.MODEL_ENV, cleanlm.TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)
    laptop.init()
    lines = iter([f + "\n" for f in fx.CUSTOMER_FORMS] + ["\n"])
    laptop.ask_forms(fx.CUSTOMER_CODE, read=lambda: next(lines, ""), tty=True, say=lambda s: None)
    return tmp_path / "laptop"


def _src(tmp_path: Path, text: str, name: str = "standup.txt") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _model_line(report: Path) -> str:
    text = report.read_text(encoding="utf-8")
    assert "## Local model" in text
    return text.split("## Local model", 1)[1].strip().splitlines()[0]


def _nick_and_names(piece):
    spans = []
    if NICK in piece:
        spans += [{"text": NICK, "kind": "person"}, {"text": "quorbel", "kind": "person"}]
    if "Kubernetes" in piece:
        spans += [{"text": "Kubernetes", "kind": "company"}, {"text": "Berlin", "kind": "place"}]
    return spans


# --------------------------------------------------------------------------- spans taken and ignored


def test_a_proposed_name_in_its_piece_becomes_a_token_and_the_rest_stays(lap, tmp_path, fakes):
    src = _src(tmp_path, LONG)
    rules = clean.run([src], PROJECT, "docs", tmp_path / "rules").outputs[0].read_text(encoding="utf-8")
    assert NICK in rules and "quorbel" in rules and "Kubernetes" in rules and "Berlin" in rules
    fake = fakes(_nick_and_names)
    res = clean.run([src], PROJECT, "docs", tmp_path / "out", llm=cleanlm.Model(fake.url))
    body = res.outputs[0].read_text(encoding="utf-8")
    # pieces of about 2,000 characters, cut at a sentence, each a stretch of the cleaned text
    assert len(fake.pieces) >= 2 and all(len(p) <= cleanlm.PIECE for p in fake.pieces)
    assert all(p in rules for p in fake.pieces) and fake.pieces[0].endswith((".", "?", "!", "\n"))
    # the name the model found in its piece became a token of wipe mode and entered the map of the run
    assert NICK not in body and "[person 1] will do it" in body
    rows = [line.split("\t") for line in res.mapping.read_text(encoding="utf-8").splitlines()]
    assert any(r[0] == "[person 1]" and r[1] == "person" and r[2] == NICK for r in rows)
    # a span that is not in its piece (it stands in another one) stays; public words and allowed terms stay
    assert "quorbel" in body and "Kubernetes" in body and "Berlin" in body
    line = _model_line(res.report)
    assert "taken person 1;" in line and "dropped as public words company 1, place 1;" in line
    assert "not in the piece person 1" in line and "missed 0" in line


def test_a_keep_word_of_the_owner_is_never_taken_from_the_model(lap, tmp_path, fakes):
    cleanlm.teach("keep", NICK)
    fake = fakes(_nick_and_names)
    res = clean.run([_src(tmp_path, SHORT)], PROJECT, "docs", tmp_path / "out", llm=cleanlm.Model(fake.url))
    assert NICK in res.outputs[0].read_text(encoding="utf-8")
    assert "dropped as public words person 1" in _model_line(res.report)


# --------------------------------------------------------------------------- loopback only, no proxy


@pytest.mark.parametrize("url", ["http://192.0.2.10:4891/v1/chat/completions", "https://example.org/v1",
                                 "http://localhost.example:4891/v1", "ftp://127.0.0.1/v1",
                                 "http://user:pw@127.0.0.1:4891/v1", "not a url"])
def test_a_url_that_is_not_loopback_is_refused_before_any_request(lap, tmp_path, monkeypatch, capsys, url):
    def refuse(*args, **kwargs):
        raise AssertionError("a connection was opened")

    src = _src(tmp_path, SHORT)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    with pytest.raises(cleanlm.LLMError) as err:
        cleanlm.Model(url)
    assert str(err.value) == cleanlm.REFUSED
    out = tmp_path / "out"
    assert cli.main(["clean", str(src), "--project", PROJECT, "--out", str(out), "--llm", url]) == 2
    printed = capsys.readouterr()
    assert printed.err.strip() == "awb clean: " + cleanlm.REFUSED and "\n" not in printed.err.strip()
    assert not out.exists()


def test_localhost_must_resolve_to_loopback_and_the_loopback_addresses_pass():
    def elsewhere(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.7", port))]

    def home(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0))]

    with pytest.raises(cleanlm.LLMError):
        cleanlm.check_url(cleanlm.DEFAULT_URL, resolve=elsewhere)
    assert cleanlm.check_url(cleanlm.DEFAULT_URL, resolve=home) == cleanlm.DEFAULT_URL
    for url in ("http://127.0.0.1:4891/v1/chat/completions", "http://[::1]:4891/v1/chat/completions"):
        assert cleanlm.check_url(url) == url


def test_a_proxy_in_the_environment_does_not_carry_the_call(lap, tmp_path, monkeypatch, fakes):
    model = fakes(_nick_and_names)
    proxy = fakes()
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, proxy.base)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    res = clean.run([_src(tmp_path, SHORT)], PROJECT, "docs", tmp_path / "out", llm=cleanlm.Model(model.url))
    assert model.pieces and proxy.requests == []
    assert NICK not in res.outputs[0].read_text(encoding="utf-8")


# --------------------------------------------------------------------------- a model that fails costs the piece only


@pytest.mark.parametrize("mode", ["slow", "500", "nonjson"])
def test_a_timeout_a_500_and_no_json_leave_the_rules_result_and_count_the_missed_pieces(lap, tmp_path, fakes,
                                                                                       mode):
    src = _src(tmp_path, SHORT)
    rules = clean.run([src], PROJECT, "docs", tmp_path / "rules").outputs[0].read_bytes()
    fake = fakes(_nick_and_names, mode)
    model = cleanlm.Model(fake.url, timeout=0.5)
    res = clean.run([src], PROJECT, "docs", tmp_path / "out", llm=model)
    assert res.outputs[0].read_bytes() == rules
    assert fake.pieces and model.missed == 1 and res.files[0].llm["missed"] == 1
    assert "pieces sent 1, missed 1;" in _model_line(res.report)


def test_without_llm_the_report_has_no_model_section(lap, tmp_path):
    res = clean.run([_src(tmp_path, SHORT)], PROJECT, "docs", tmp_path / "out")
    assert "Local model" not in res.report.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- counts only


def test_the_report_and_the_terminal_hold_counts_only(lap, tmp_path, fakes, capsys):
    fake = fakes(_nick_and_names)
    src = _src(tmp_path, SHORT)
    out = tmp_path / "out"
    assert cli.main(["clean", str(src), "--project", PROJECT, "--out", str(out), "--llm", fake.url]) == 0
    printed = capsys.readouterr()
    (report,) = out.glob("clean-report-*.md")
    text = report.read_text(encoding="utf-8")
    assert "taken person 1" in _model_line(report)
    assert NICK.casefold() not in text.casefold() and "quorbel" not in text
    assert NICK.casefold() not in (printed.out + printed.err).casefold()
    assert "local model: 1 piece sent, 0 missed" in printed.out


# --------------------------------------------------------------------------- the local corrections


def test_teach_keep_and_wipe_change_the_next_run_and_the_file_is_600(lap, tmp_path, capsys):
    src = _src(tmp_path, "Julia runs the numerics on the cluster.\n" + SHORT)
    before = clean.run([src], PROJECT, "docs", tmp_path / "o1").outputs[0].read_text(encoding="utf-8")
    assert "Julia" not in before and NICK in before
    assert cli.main(["clean", "teach", "keep", "Julia"]) == 0
    assert cli.main(["clean", "teach", "wipe", NICK, "--kind", "company"]) == 0
    path = lap / "vault" / "teach.tsv"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    capsys.readouterr()
    res = clean.run([src], PROJECT, "docs", tmp_path / "o2")
    after = res.outputs[0].read_text(encoding="utf-8")
    assert "Julia runs the numerics" in after
    assert NICK not in after and "[company 1] will do it" in after
    assert NICK not in res.report.read_text(encoding="utf-8")
    assert cli.main(["clean", "teach", "list"]) == 0
    listed = capsys.readouterr().out
    assert "keep 1" in listed and "wipe company 1" in listed and "wipe person 0" in listed
    assert "Julia" not in listed and NICK not in listed
    # a newer entry of the same word replaces the older one
    assert cli.main(["clean", "teach", "wipe", NICK]) == 0
    assert cleanlm.wipe_words() == [(NICK, "person")]
    assert cli.main(["clean", "teach", "keep", "x"]) == 2


class _Bucket:
    """The bucket in memory, the calls of obs.Client that put, fetch and the folder lookup make."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def list(self, prefix="", delimiter=""):
        objects, prefixes = [], set()
        for key, data in sorted(self.objects.items()):
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if delimiter and delimiter in rest:
                prefixes.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
                continue
            objects.append((key, len(data), "etag-%d" % len(data)))
        return obs.Listing(objects, sorted(prefixes))

    def head(self, key):
        return {"size": len(self.objects[key])} if key in self.objects else None

    def put_bytes(self, key, data, *args, **kwargs):
        self.objects[key] = bytes(data)

    def put_file(self, key, path, *args, **kwargs):
        self.objects[key] = Path(path).read_bytes()

    def get(self, key, dest, if_match=None):
        Path(dest).write_bytes(self.objects[key])


def test_put_fetch_and_update_never_read_the_teach_file(lap, tmp_path, monkeypatch):
    cleanlm.teach("wipe", NICK)
    res = clean.run([_src(tmp_path, SHORT)], PROJECT, "docs", tmp_path / "out")
    teach = str(lap / "vault" / "teach.tsv")
    opened: list[str] = []

    def watch(original):
        def inner(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                opened.append(os.fsdecode(os.fspath(file)))
            return original(file, *args, **kwargs)
        return inner

    def refuse(*args, **kwargs):
        raise AssertionError("the teach file was read")

    monkeypatch.setattr(builtins, "open", watch(builtins.open))
    monkeypatch.setattr(io, "open", watch(io.open))
    monkeypatch.setattr(os, "open", watch(os.open))
    monkeypatch.setattr(cleanlm, "teach_entries", refuse)
    monkeypatch.setattr(cleanlm, "teach_path", refuse)
    bucket = _Bucket()
    bucket.objects["2026-11/%s/" % PROJECT] = b""
    bucket.objects["2026-11/%s/out/offer.md" % PROJECT] = b"# offer\n"
    assert clean.put_files(PROJECT, res.outputs, client=bucket, say=lambda s: None)
    assert cleanfetch.fetch(PROJECT, tmp_path / "fetched", client=bucket, say=lambda s: None) == 0
    rules = tmp_path / "pkg" / "rules"
    rules.mkdir(parents=True)
    fresh = b"alpha\n"

    def fetch(url, limit=0):
        if url == clean.TREE_URL:
            return json.dumps({"tree": [{"path": "rules/common-words.txt", "sha": clean.git_blob_id(fresh)}]}).encode()
        return fresh

    assert clean.update(rules, fetch=fetch)
    assert opened and teach not in opened
    assert not any(NICK.encode() in data for data in bucket.objects.values())


# --------------------------------------------------------------------------- awb clean measure


PACK_MODULE = '''
def txt(name, text):
    def build(inbox):
        p = inbox / name
        p.write_text(text, encoding="utf-8")
        return p
    return build


CASES = [
    {"id": "nick", "values": [%(nick)r], "keep": ["restore test"], "build": txt("a.txt", %(text)r)},
    {"id": "plain", "values": [], "keep": ["backup job"], "build": txt("b.txt", "the backup job runs at night\\n")},
]
'''


def test_measure_prints_both_columns_and_a_proposed_leak_drops_the_leak_count_by_one(lap, tmp_path, fakes,
                                                                                    monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "cases_tiny.py").write_text(PACK_MODULE % {"nick": NICK, "text": SHORT}, encoding="utf-8")
    fake = fakes(_nick_and_names)
    home = os.environ[laptop.HOME_ENV]
    lines: list[str] = []
    res = cleanlm.measure(fake.url, use_model=True, pack=pack, say=lines.append)
    assert os.environ[laptop.HOME_ENV] == home
    row = res["modules"]["tiny"]
    assert row["cases"] == 2 and row["rules"]["leak"] == 1 and row["model"]["leak"] == 0
    assert row["rules"]["loss"] == row["model"]["loss"] == 0
    assert "with model" in lines[1] and "rules" in lines[1]
    tiny = next(line for line in lines if line.startswith("tiny"))
    assert tiny.split() == ["tiny", "2", "|", "1", "0", "|", "0", "0"]
    assert any(line.startswith("model: 2 pieces sent, 0 missed") for line in lines)
    assert NICK not in "\n".join(lines)
    alone: list[str] = []
    cleanlm.measure(pack=pack, say=alone.append)
    assert next(line for line in alone if line.startswith("tiny")).split() == ["tiny", "2", "|", "1", "0", "|", "-",
                                                                              "-"]
    assert not fake.pieces[2:]
