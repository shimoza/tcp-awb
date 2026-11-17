"""awb/tcp/doc.py and the op doc_get of the key service: public documentation read on the owner's side (task DF).

Every fetch here goes to a stubbed getter in place of the network: it records what was asked and answers with a
canned page. The hosts come from a short invented list or from rules/allow-domains.txt itself.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import cli
from awb.tcp import doc, keys

ROOT = Path(__file__).resolve().parent.parent
HOSTS = ["learn.microsoft.com", "docs.example.org", "example.com"]
PROJECT = "tcp-ab2c"
PAGE = ("<html><head><title>Credential guard overview</title><script>steal()</script><style>p{}</style></head>"
        "<body><nav>menu menu</nav><h1>Credential guard overview</h1><p>Credential guard uses <code>VBS</code> to "
        "isolate secrets.</p><h2>Requirements</h2><ul><li>UEFI 2.3.1</li><li>TPM 2.0<ol><li>firmware</li></ol></li>"
        "</ul><pre>Get-CimInstance\n  -ClassName Win32_DeviceGuard</pre><table><tr><th>Edition</th><th>Supported"
        "</th></tr><tr><td>Enterprise</td><td>yes</td></tr></table></body></html>")


class Resp:
    def __init__(self, body: bytes):
        self.stream = io.BytesIO(body)
        self.closed = False

    def read(self, n=-1):
        return self.stream.read(n)

    def close(self):
        self.closed = True


class Getter:
    """Answers by (host, path): (status, headers, body). Records every request."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.asked: list[tuple[str, str]] = []

    def __call__(self, host, path, timeout=None):
        self.asked.append((host, path))
        status, headers, body = self.pages.get((host, path), (404, {"content-type": "text/plain"}, b"no"))
        return status, {k.lower(): v for k, v in headers.items()}, Resp(body)


def html_page(body: str = PAGE, **headers):
    return 200, dict({"content-type": "text/html; charset=utf-8"}, **headers), body.encode("utf-8")


# --------------------------------------------------------------------------- the checks


def test_an_allowed_host_passes_and_html_becomes_readable_text():
    g = Getter({("learn.microsoft.com", "/en-us/windows/security/credential-guard"): html_page()})
    page = doc.fetch("https://learn.microsoft.com/en-us/windows/security/credential-guard", getter=g, hosts=HOSTS)
    assert page["status"] == 200 and page["kind"] == "html" and page["host"] == "learn.microsoft.com"
    text = page["text"]
    assert text.startswith("# Credential guard overview\n")
    assert "## Requirements" in text and "- UEFI 2.3.1" in text and "  1. firmware" in text
    assert "Credential guard uses `VBS` to isolate secrets." in text
    assert "```\nGet-CimInstance\n  -ClassName Win32_DeviceGuard\n```" in text
    assert "| Edition | Supported |" in text and "| Enterprise | yes |" in text
    assert "steal()" not in text and "p{}" not in text and "menu" not in text


def test_the_hosts_are_the_lines_of_the_rules_file_exactly():
    hosts = doc.allowed_hosts()
    assert "learn.microsoft.com" in hosts and "docs.llmhub.t-systems.net" in hosts
    assert all(not h.startswith("#") and " " not in h for h in hosts)
    # exact match: a sub-host of a listed host is not listed
    assert "microsoft.com" in hosts
    with pytest.raises(doc.DocError, match="not on rules/allow-domains.txt"):
        doc.check_url("https://evil.microsoft.com/x")
    doc.check_url("https://learn.microsoft.com/en-us/")


@pytest.mark.parametrize("url, rule", [
    ("https://attacker.example.net/x", "not on rules/allow-domains.txt"),
    ("https://learn.microsoft.com.attacker.test/x", "not on rules/allow-domains.txt"),
    ("http://learn.microsoft.com/x", "https only"),
    ("ftp://learn.microsoft.com/x", "https only"),
    ("https://learn.microsoft.com/x?data=secret", "no query string"),
    ("https://learn.microsoft.com/x?", "no query string"),
    ("https://learn.microsoft.com/x#part", "no fragment"),
    ("https://user:pw@learn.microsoft.com/x", "no user info"),
    ("https://learn.microsoft.com@attacker.test/x", "no user info"),
    ("https://learn.microsoft.com:8443/x", "no port"),
    ("https://learn.microsoft.com./x", "plain host name"),
    ("https://10.0.0.1/x", "not on rules/allow-domains.txt"),
    ("https://learn.microsoft.com/" + "a" * 300, "longer than 300"),
    ("https://learn.microsoft.com/a b", "plain ASCII"),
    ("https://learn.microsoft.com/a\"b", "not URL-safe"),
    ("https://learn.microsoft.com/a/../b", "not URL-safe"),
    ("https://learn.microsoft.com/a%zz", "not URL-safe"),
    ("https://learn.microsoft.com/a<b>", "not URL-safe"),
])
def test_a_url_outside_the_rules_is_refused_before_anything_is_sent(url, rule):
    g = Getter({})
    with pytest.raises(doc.DocError, match=rule) as err:
        doc.fetch(url, getter=g, hosts=HOSTS)
    assert str(err.value).startswith("refused: ")
    assert g.asked == []


def test_a_redirect_is_followed_on_the_list_and_refused_off_it():
    g = Getter({
        ("learn.microsoft.com", "/a"): (301, {"location": "/b"}, b""),
        ("learn.microsoft.com", "/b"): (302, {"location": "https://docs.example.org/c"}, b""),
        ("docs.example.org", "/c"): html_page("<p>landed</p>"),
        ("learn.microsoft.com", "/out"): (302, {"location": "https://attacker.example.net/collect"}, b""),
        ("learn.microsoft.com", "/q"): (307, {"location": "https://docs.example.org/c?x=1"}, b""),
        ("learn.microsoft.com", "/plain"): (301, {"location": "http://docs.example.org/c"}, b""),
    })
    page = doc.fetch("https://learn.microsoft.com/a", getter=g, hosts=HOSTS)
    assert page["url"] == "https://docs.example.org/c" and page["text"].strip() == "landed"
    for start, rule in (("/out", "not on rules/allow-domains.txt"), ("/q", "no query string"),
                        ("/plain", "https only")):
        with pytest.raises(doc.DocError, match="redirect to a URL the rules refuse") as err:
            doc.fetch("https://learn.microsoft.com" + start, getter=g, hosts=HOSTS)
        assert rule in str(err.value)
    assert ("attacker.example.net", "/collect") not in g.asked


def test_at_most_three_redirects():
    pages = {("example.com", "/%d" % n): (302, {"location": "/%d" % (n + 1)}, b"") for n in range(5)}
    pages[("example.com", "/3")] = html_page("<p>three</p>")
    assert doc.fetch("https://example.com/0", getter=Getter(pages), hosts=HOSTS)["text"].strip() == "three"
    pages[("example.com", "/3")] = (302, {"location": "/4"}, b"")
    g = Getter(pages)
    with pytest.raises(doc.DocError, match="more than 3 redirects"):
        doc.fetch("https://example.com/0", getter=g, hosts=HOSTS)
    assert len(g.asked) == 4


def test_the_size_cap():
    big = b"x" * (doc.MAX_BYTES + 1)
    told = Getter({("example.com", "/told"): (200, {"content-type": "text/plain",
                                                     "content-length": str(len(big))}, big)})
    with pytest.raises(doc.DocError, match="larger than 5 MB"):
        doc.fetch("https://example.com/told", getter=told, hosts=HOSTS)
    untold = Getter({("example.com", "/untold"): (200, {"content-type": "text/plain"}, big)})
    with pytest.raises(doc.DocError, match="larger than 5 MB"):
        doc.fetch("https://example.com/untold", getter=untold, hosts=HOSTS)
    exact = Getter({("example.com", "/exact"): (200, {"content-type": "text/plain"}, b"y" * doc.MAX_BYTES)})
    assert doc.fetch("https://example.com/exact", getter=exact, hosts=HOSTS)["size"] == doc.MAX_BYTES


@pytest.mark.parametrize("ctype", ["image/png", "", "application/zip", "text/javascript"])
def test_a_type_that_is_not_text_is_refused(ctype):
    g = Getter({("example.com", "/f"): (200, {"content-type": ctype}, b"%PDF-1.7")})
    with pytest.raises(doc.DocError, match="not a text type"):
        doc.fetch("https://example.com/f", getter=g, hosts=HOSTS)


@pytest.mark.parametrize("ctype, kind", [("text/markdown", "markdown"), ("text/plain; charset=utf-8", "text"),
                                         ("application/json", "json"), ("application/xhtml+xml", "html")])
def test_the_text_types_are_taken(ctype, kind):
    g = Getter({("example.com", "/f"): (200, {"content-type": ctype}, b"# title\n")})
    assert doc.fetch("https://example.com/f", getter=g, hosts=HOSTS)["kind"] == kind


def test_an_encoded_body_and_an_error_status_are_refused():
    g = Getter({("example.com", "/gz"): (200, {"content-type": "text/html", "content-encoding": "gzip"}, b"\x1f\x8b"),
                ("example.com", "/gone"): (410, {"content-type": "text/html"}, b"gone")})
    with pytest.raises(doc.DocError, match="encoded body"):
        doc.fetch("https://example.com/gz", getter=g, hosts=HOSTS)
    with pytest.raises(doc.DocError, match="answered 410"):
        doc.fetch("https://example.com/gone", getter=g, hosts=HOSTS)


def test_the_request_carries_no_cookie_no_auth_and_the_fixed_user_agent(monkeypatch):
    seen = {}

    class Conn:
        def __init__(self, host, port, timeout=None, context=None):
            seen.update(host=host, port=port, context=context)

        def request(self, method, path, headers=None):
            seen.update(method=method, path=path, headers=dict(headers or {}))

        def getresponse(self):
            return SimpleNamespace(status=200, getheaders=lambda: [("Content-Type", "text/plain")])

    import http.client

    monkeypatch.setattr(http.client, "HTTPSConnection", Conn)
    status, headers, _ = doc.https_get("learn.microsoft.com", "/x")
    assert (status, headers) == (200, {"content-type": "text/plain"})
    assert seen["method"] == "GET" and seen["port"] == 443 and seen["context"] is not None
    names = {k.lower() for k in seen["headers"]}
    assert "cookie" not in names and "authorization" not in names
    assert seen["headers"]["User-Agent"] == doc.USER_AGENT and "Architect-Workbench" in doc.USER_AGENT


# --------------------------------------------------------------------------- rate and ledger


def test_the_rate_limit_per_minute_and_per_day(tmp_path):
    lim = doc.Limiter(lambda: tmp_path)
    for n in range(30):
        assert lim.take(PROJECT, now=100.0 + n) is None
    assert "30 fetches a minute" in lim.take(PROJECT, now=130.0)
    assert lim.take("tcp-zz9z", now=130.0) is None            # per project
    assert lim.take(PROJECT, now=161.0) is None                # the first one left the minute
    day = doc.Limiter(lambda: tmp_path, per_minute=10 ** 6)
    for n in range(500):
        assert day.take(PROJECT, now=float(n)) is None
    assert "500 fetches a day" in day.take(PROJECT, now=600.0)


def test_the_count_of_the_day_survives_a_restart_from_the_ledger(tmp_path):
    for _ in range(499):
        doc.ledger_line(tmp_path, PROJECT, "example.com", "/x", 10, "200")
    doc.ledger_line(tmp_path, PROJECT, "example.com", "/x", None, "rate")
    doc.ledger_line(tmp_path, PROJECT, "-", "-", None, "refused")
    lim = doc.Limiter(lambda: tmp_path)
    assert lim.take(PROJECT) is None
    assert "500 fetches a day" in lim.take(PROJECT)


@contextlib.contextmanager
def service(tmp_path, getter, hosts=HOSTS):
    d = Path(tempfile.mkdtemp(prefix="awbd", dir="/tmp"))
    vault = tmp_path / "vault"
    s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log",
                     paths_fn=lambda: SimpleNamespace(vault=vault))
    s._active_project = lambda code: code == PROJECT
    s.doc_getter = getter
    s.doc_hosts = hosts
    s.start()
    try:
        yield s
    finally:
        s.stop()
        shutil.rmtree(d, ignore_errors=True)


def ledger_rows(tmp_path) -> list[list[str]]:
    files = sorted((tmp_path / "log").glob("doc-*.tsv"))
    return [line.split("\t") for f in files for line in f.read_text(encoding="utf-8").splitlines()]


def test_a_fetch_through_the_call_socket_streams_the_text_and_the_ledger_carries_no_content(tmp_path):
    g = Getter({("learn.microsoft.com", "/cg"): html_page()})
    with service(tmp_path, g) as s:
        out = tmp_path / "got.txt"
        answer = keys.request(s.call_path, {"op": "doc_get", "url": "https://learn.microsoft.com/cg",
                                            "project": PROJECT}, download=out)
        assert answer["ok"] and answer["url"] == "https://learn.microsoft.com/cg" and answer["kind"] == "html"
        assert answer["fetched"].endswith(" UTC") and len(answer["fetched"]) == len("2026-10-22 10:00 UTC")
        assert "Credential guard uses `VBS`" in out.read_text(encoding="utf-8")
        refused = keys.request(s.call_path, {"op": "doc_get", "url": "https://learn.microsoft.com/cg?leak=1",
                                             "project": PROJECT})
        assert not refused["ok"] and refused["error"] == "refused: no query string"
        unknown = keys.request(s.call_path, {"op": "doc_get", "url": "https://learn.microsoft.com/cg",
                                             "project": "tcp-zz9z"})
        assert not unknown["ok"] and "active project" in unknown["error"]
        assert not list((tmp_path / "vault" / "tmp").glob("awb-doc-*"))     # the stream file is gone
    rows = ledger_rows(tmp_path)
    assert len(rows) == 2
    ok, no = rows
    assert len(ok) == 6 and ok[1:] == [PROJECT, "learn.microsoft.com", "/cg", str(len(PAGE.encode())), "200"]
    assert ok[0][:10].count("-") == 2
    assert no[1:] == [PROJECT, "-", "-", "-", "refused"]
    whole = "".join((tmp_path / "log").joinpath(f.name).read_text() for f in (tmp_path / "log").glob("*"))
    for word in ("Credential", "VBS", "leak", "Requirements", "steal"):
        assert word not in whole


def test_the_rate_limit_through_the_service(tmp_path):
    g = Getter({("example.com", "/x"): html_page("<p>x</p>")})
    with service(tmp_path, g) as s:
        s.doc_limiter = doc.Limiter(s._doc_ledger_dir, per_minute=2)
        req = {"op": "doc_get", "url": "https://example.com/x", "project": PROJECT}
        assert s._call_socket(dict(req), os.getuid())[0]["ok"]
        assert s._call_socket(dict(req), os.getuid())[0]["ok"]
        third = s._call_socket(dict(req), os.getuid())
        assert third == ({"ok": False, "error": "refused: at most 2 fetches a minute per project"}, None)
    assert len(g.asked) == 2
    assert [r[5] for r in ledger_rows(tmp_path)] == ["200", "200", "rate"]


def test_a_session_uses_the_project_of_its_key_token_not_the_one_it_names(tmp_path):
    g = Getter({("example.com", "/x"): html_page("<p>x</p>")})
    with service(tmp_path, g) as s:
        other = os.getuid() + 4711
        s._console_uid = lambda: None
        s._grant_tokens = lambda: {"tcp-q7m4": "a" * 64}
        caller = s._caller_project({"token": "a" * 64}, other)
        assert caller == "tcp-q7m4"
        answer, stream = s._call_socket({"op": "doc_get", "url": "https://example.com/x", "project": PROJECT},
                                        other, caller=caller)
        assert answer["ok"]
        stream.unlink()
    assert ledger_rows(tmp_path)[0][1] == "tcp-q7m4"


def test_the_work_user_cannot_reach_the_admin_ops_through_the_call_socket(tmp_path):
    g = Getter({})
    with service(tmp_path, g) as s:
        work = os.getuid() + 4711
        s._console_uid = lambda: None
        for op in keys.ADMIN_OPS:
            if op == "ping":
                continue
            answer = s._call_socket({"op": op, "url": "https://example.com/x", "tenants": {}}, work)
            answer = answer[0] if isinstance(answer, tuple) else answer
            assert not answer.get("ok"), op
            assert answer.get("error") in ("unknown operation", "refused"), op
        # the admin socket itself refuses any uid but the owner's
        assert s._admin({"op": "lock"}, work) == {"ok": False, "error": "refused"}
        # doc_get is no way into an admin op: the op field is read once and nothing else is dispatched
        answer, _ = s._call_socket({"op": "doc_get", "url": "https://example.com/x", "admin": "lock"}, work)
        assert not answer["ok"] and "active project" in answer["error"]
    assert g.asked == []


# --------------------------------------------------------------------------- the session side


@pytest.fixture
def project_folder(tmp_path, monkeypatch):
    folder = tmp_path / PROJECT
    folder.mkdir()
    (folder / "SCOPE.md").write_text("# scope\n\n- code: %s\n- customer: none\n" % PROJECT, encoding="utf-8")
    monkeypatch.chdir(folder)
    return folder


def test_awb_doc_fetch_prints_the_text_with_its_source_and_date(tmp_path, project_folder, monkeypatch, capsys):
    g = Getter({("learn.microsoft.com", "/cg"): html_page()})
    with service(tmp_path, g) as s:
        monkeypatch.setattr(keys, "call_socket", lambda: s.call_path)
        assert cli.main(["doc", "fetch", "https://learn.microsoft.com/cg"]) == 0
        out = capsys.readouterr().out
        first = out.splitlines()[0]
        assert first.startswith("source: https://learn.microsoft.com/cg, fetched 20") and first.endswith(" UTC")
        assert "# Credential guard overview" in out
        assert cli.main(["doc", "fetch", "https://learn.microsoft.com/cg", "--out", "notes/cg.md"]) == 0
        written = (project_folder / "notes" / "cg.md").read_text(encoding="utf-8")
        assert written.startswith("source: https://learn.microsoft.com/cg, fetched ")
        assert "notes/cg.md" in capsys.readouterr().out
        assert cli.main(["doc", "fetch", "https://learn.microsoft.com/cg", "--out", str(tmp_path / "out.md")]) == 2
        assert capsys.readouterr().err.strip() == "refused: --out names a file inside the project folder"
        assert not (tmp_path / "out.md").exists()


def test_awb_doc_fetch_refuses_with_one_line_naming_the_rule(project_folder, monkeypatch, capsys):
    monkeypatch.setattr(keys, "request", lambda *a, **k: pytest.fail("nothing is sent for a refused URL"))
    for url, line in (("https://learn.microsoft.com/x?q=1", "refused: no query string"),
                      ("http://learn.microsoft.com/x", "refused: https only"),
                      ("https://attacker.example.net/x",
                       "refused: the host is not on rules/allow-domains.txt (awb doc hosts); leave one owner item in "
                       "OPEN.md under the owner heading: Steering: allow documentation host attacker.example.net "
                       "(vendor, why)")):
        assert cli.main(["doc", "fetch", url]) == 1
        err = capsys.readouterr().err
        assert err == line + "\n"


def test_awb_doc_fetch_outside_a_project_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(keys, "request", lambda *a, **k: pytest.fail("nothing is sent outside a project"))
    assert cli.main(["doc", "fetch", "https://learn.microsoft.com/x"]) == 2
    assert capsys.readouterr().err.startswith("refused: ")


def test_awb_doc_hosts_lists_the_rules_file(capsys):
    assert cli.main(["doc", "hosts"]) == 0
    listed = capsys.readouterr().out.split()
    assert listed == doc.allowed_hosts() and "learn.microsoft.com" in listed


def test_the_rules_file_carries_the_official_documentation_hosts():
    hosts = doc.allowed_hosts()
    for host in ("learn.microsoft.com", "docs.aws.amazon.com", "cloud.google.com", "docs.redhat.com",
                 "access.redhat.com", "documentation.ubuntu.com", "help.ubuntu.com", "www.cisco.com",
                 "techdocs.broadcom.com", "docs.vmware.com", "docs.nvidia.com", "kubernetes.io",
                 "registry.terraform.io", "developer.hashicorp.com", "docs.otc.t-systems.com",
                 "docs-beta.otc.t-systems.com"):
        assert host in hosts, host
        doc.check_url("https://%s/" % host)


def test_a_refusal_off_the_list_from_the_service_names_the_owner_item_too(tmp_path, project_folder, monkeypatch,
                                                                           capsys):
    monkeypatch.setattr(doc, "check_url", lambda url, hosts=None: ("docs.example.org", "/x"))
    monkeypatch.setattr(keys, "request", lambda *a, **k: {"ok": False, "error": "refused: a redirect to a URL the "
                                                          "rules refuse (the host is not on rules/allow-domains.txt "
                                                          "(awb doc hosts))"})
    assert cli.main(["doc", "fetch", "https://docs.example.org/x"]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and err.rstrip().endswith("Steering: allow documentation host docs.example.org "
                                                           "(vendor, why)")


def test_the_work_rules_and_the_drafting_skill_say_to_fetch_documentation():
    rules = " ".join((ROOT / "seal" / "work-claude" / "CLAUDE.md").read_text(encoding="utf-8").split())
    assert "Public documentation of an allowed host is read with `awb doc fetch URL`" in rules
    assert "quoted with its source and date" in rules
    assert "never stated from memory when the page can be fetched" in rules
    assert "an unlisted host: an owner item in OPEN.md" in rules
    for name in ("plugin/skills/drafting/SKILL.md", "seal/work-claude/skills/drafting/SKILL.md"):
        assert "`awb doc fetch URL`" in (ROOT / name).read_text(encoding="utf-8")
    allow = (ROOT / "seal" / "egress" / "ALLOW.md").read_text(encoding="utf-8")
    assert "doc_get" in allow and "awb doc fetch" in allow


def test_a_gzip_body_is_unpacked_when_the_host_ignores_identity():
    import gzip
    page = b"<html><body><h1>volume types</h1><p>ultra-high</p></body></html>"
    g = Getter({("example.com", "/z"): (200, {"content-type": "text/html", "content-encoding": "gzip"},
                                        gzip.compress(page))})
    got = doc.fetch("https://example.com/z", getter=g, hosts=HOSTS)
    assert "volume types" in got["text"] and "ultra-high" in got["text"]


def test_a_gzip_body_that_unpacks_past_the_cap_or_breaks_off_is_refused():
    import gzip
    whole = gzip.compress(b"a" * 5000)
    g = Getter({("example.com", "/big"): (200, {"content-type": "text/plain", "content-encoding": "gzip"}, whole),
                ("example.com", "/cut"): (200, {"content-type": "text/plain", "content-encoding": "gzip"},
                                          whole[:len(whole) // 2]),
                ("example.com", "/br"): (200, {"content-type": "text/plain", "content-encoding": "br"}, b"x")})
    with pytest.raises(doc.DocError, match="larger than"):
        doc.fetch("https://example.com/big", getter=g, hosts=HOSTS, cap=1000)
    with pytest.raises(doc.DocError, match="not valid gzip"):
        doc.fetch("https://example.com/cut", getter=g, hosts=HOSTS)
    with pytest.raises(doc.DocError, match="encoded body"):
        doc.fetch("https://example.com/br", getter=g, hosts=HOSTS)


# Review regressions. These use isolated files and in-process stubs, including on a build export.

def _review_answer(monkeypatch, body="public guide\n", action=None, **fields):
    def request(*args, download, **kwargs):
        download.write_text(body, encoding="utf-8")
        if action:
            action(download)
        return dict({"ok": True, "url": "https://learn.microsoft.com/x",
                     "fetched": "2026-11-16 10:00 UTC", "kind": "text"}, **fields)
    monkeypatch.setattr(keys, "request", request)


def _review_fetch(out=None):
    return doc.main(["fetch", "https://learn.microsoft.com/x"] + (["--out", str(out)] if out else []))


def test_review_encoded_traversal_and_controls_never_reach_getter():
    getter = Getter({})
    for path in ("/.%2e/x", "/%2e./x", "/a%2f..%2fx", "/%252e%252e/x", "/%00x", "/%0dx", "/a%5cb"):
        with pytest.raises(doc.DocError, match="not URL-safe"):
            doc.fetch("https://example.com" + path, getter=getter, hosts=HOSTS)
    assert getter.asked == []


def test_review_service_rejects_malformed_requests_and_project_newlines(tmp_path):
    getter = Getter({("example.com", "/x"): html_page()})
    for req, project in (([], PROJECT), ({"url": "https://example.com/x", "links": "yes"}, PROJECT),
                         ({"url": "https://example.com/x"}, PROJECT + "\n")):
        answer, stream = doc.serve(req, project, doc.Limiter(lambda: None), None,
                                   getter=getter, hosts=HOSTS, tmp_dir=tmp_path)
        if stream:
            stream.unlink()
        assert answer["ok"] is False and stream is None
    assert getter.asked == []


def test_review_response_read_errors_are_private_and_close_response():
    secret = "pass" + "word=never-print-7391"
    response = Resp(b"")
    response.read = lambda n: (_ for _ in ()).throw(OSError(secret))
    with pytest.raises(doc.DocError) as error:
        doc.fetch("https://example.com/x", hosts=HOSTS,
                  getter=lambda *a: (200, {"content-type": "text/plain"}, response))
    assert secret not in str(error.value) and response.closed
    assert error.value.host == "example.com" and error.value.path == "/x"


def test_review_getter_docerror_cannot_echo_values():
    secret = "pass" + "word=never-print-7391"
    def getter(*args):
        raise doc.DocError(secret)
    with pytest.raises(doc.DocError) as error:
        doc.fetch("https://example.com/x", getter=getter, hosts=HOSTS)
    assert secret not in str(error.value)


def test_review_oversized_numeric_headers_fail_as_docerror_and_close():
    response = Resp(b"body")
    with pytest.raises(doc.DocError):
        doc.fetch("https://example.com/x", hosts=HOSTS,
                  getter=lambda *a: (200, {"content-type": "text/plain", "content-length": "9" * 5000}, response))
    assert response.closed and response.stream.tell() == 0


def test_review_gzip_trailing_members_and_junk_are_not_silently_dropped():
    import gzip
    for tail in (gzip.compress(b"hidden second member"), b"junk"):
        with pytest.raises(doc.DocError, match="gzip"):
            doc._gunzip_capped(gzip.compress(b"first") + tail, 1000)


def test_review_malformed_links_are_skipped_and_valid_links_survive():
    lines, root = doc.page_links('<a href="https://[broken">broken</a><a href="/guide">guide</a>',
                                 "https://example.com/", HOSTS)
    assert lines == ["- guide: https://example.com/guide"] and root is None


def test_review_link_credentials_and_unsafe_help_roots_are_withheld():
    secret = "pass" + "word-7391"
    page = ('<html data-mc-path-to-help-system="https://reader:' + secret + '@example.com/">'
            '<a href="https://reader:' + secret + '@example.com/guide">guide</a></html>')
    lines, root = doc.page_links(page, "https://example.com/", HOSTS)
    assert not lines and root is None


def test_review_hidden_html_titles_and_mismatched_end_tags_stay_hidden():
    secret = "pass" + "word=never-print-7391"
    for hidden in ("<form><title>" + secret + "</title></form>",
                   "<form></nav>" + secret + "</form>"):
        text = doc.html_text(hidden + "<p>visible</p>")
        assert secret not in text and "visible" in text
    assert "visible" in doc.html_text("<svg/><p>visible</p>")


def test_review_host_lists_refuse_links_invalid_utf8_and_oversize(tmp_path):
    target = tmp_path / "target"
    target.write_text("example.com\n")
    link = tmp_path / "link"
    link.symlink_to(target)
    for reader in (doc.allowed_hosts, doc.extra_hosts):
        assert reader(link) == []
        target.write_bytes(b"\xff")
        assert reader(target) == []
        target.write_text("example.com\n" + "x" * (1024 * 1024))
        assert reader(target) == []
        target.write_text("example.com\n")


def test_review_host_lists_filter_values_that_are_not_hosts(tmp_path):
    secret = "pass" + "word=never-print-7391"
    path = tmp_path / "hosts"
    path.write_text("example.com\n" + secret + "\nhttps://example.com\n*.example.com\n")
    assert doc.allowed_hosts(path) == doc.extra_hosts(path) == ["example.com"]


def test_review_host_list_fifo_is_never_read_as_a_stream(tmp_path, monkeypatch):
    path = tmp_path / "hosts"
    os.mkfifo(path)
    # Fail immediately on the old blocking path, rather than hanging the suite on a FIFO.
    def unsafe_read(*a, **kw):
        pytest.fail("host list opened as an unbounded stream")
    monkeypatch.setattr(Path, "read_text", unsafe_read)
    assert doc.allowed_hosts(path) == doc.extra_hosts(path) == []


def test_review_host_add_refuses_links_and_fifos(tmp_path, monkeypatch):
    from awb import config
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    target = tmp_path / "target"
    target.write_text("example.com\n")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(doc.DocError):
        doc.add_host("docs.vendor-q7m4.example", path=link)
    assert target.read_text() == "example.com\n"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        # Avoid the old reader hanging before it gets to the append.
        monkeypatch.setattr(doc, "extra_hosts", lambda path=None: [])
        with pytest.raises(doc.DocError):
            doc.add_host("docs.vendor-q7m4.example", path=fifo)
    finally:
        os.close(fd)


def test_review_host_add_serializes_duplicate_checks(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from awb import config
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    path = tmp_path / "hosts"
    barrier, local = threading.Barrier(2), threading.local()
    original = doc.check_new_host
    def checked(*args, **kwargs):
        result = original(*args, **kwargs)
        if not getattr(local, "checked", False):
            local.checked = True
            barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(doc, "check_new_host", checked)
    def add():
        try:
            return doc.add_host("docs.vendor-q7m4.example", path=path)
        except doc.DocError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: add(), range(2)))
    assert results.count(None) == 1
    assert len(path.read_text().splitlines()) == 1


def test_review_host_add_preserves_a_final_unterminated_row(tmp_path, monkeypatch):
    from awb import config
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    path = tmp_path / "hosts"
    path.write_text("example.com")
    doc.add_host("docs.vendor-q7m4.example", path=path)
    assert doc.extra_hosts(path) == ["example.com", "docs.vendor-q7m4.example"]


def test_review_host_write_failure_does_not_echo_oserror_values(tmp_path, monkeypatch):
    import builtins
    from awb import config
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    secret = "pass" + "word=never-print-7391"
    path = tmp_path / "hosts"
    old_open, old_os_open = builtins.open, os.open
    def fail_open(file, *args, **kwargs):
        if Path(file) == path:
            raise OSError(13, secret, str(path))
        return old_open(file, *args, **kwargs)
    def fail_os_open(file, *args, **kwargs):
        if file == path.name:
            raise OSError(13, secret, str(path))
        return old_os_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", fail_open)
    monkeypatch.setattr(os, "open", fail_os_open)
    with pytest.raises(doc.DocError) as error:
        doc.add_host("docs.vendor-q7m4.example", path=path)
    assert secret not in str(error.value)


def test_review_unreadable_rate_ledger_fails_closed(tmp_path, monkeypatch):
    import builtins
    path = doc._ledger_path(tmp_path)
    path.write_bytes(b"\xff")
    with pytest.raises(doc.DocError):
        doc.Limiter(lambda: tmp_path).take(PROJECT)
    path.write_text("")
    original, os_original = builtins.open, os.open
    def denied(file, *a, **kw):
        if Path(file) == path:
            raise PermissionError("unreadable")
        return original(file, *a, **kw)
    def os_denied(file, *a, **kw):
        if file == path.name:
            raise PermissionError("unreadable")
        return os_original(file, *a, **kw)
    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(os, "open", os_denied)
    with pytest.raises(doc.DocError):
        doc.Limiter(lambda: tmp_path).take(PROJECT)


def test_review_rate_ledger_links_do_not_reset_or_supply_counts(tmp_path):
    target = tmp_path / "target"
    target.write_text("")
    doc._ledger_path(tmp_path).symlink_to(target)
    with pytest.raises(doc.DocError):
        doc.Limiter(lambda: tmp_path).take(PROJECT)


def test_review_ledger_writes_do_not_follow_parent_links(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "log"
    linked.symlink_to(outside, target_is_directory=True)
    doc.ledger_line(linked, PROJECT, "example.com", "/x", 1, "200")
    assert list(outside.iterdir()) == []


def test_review_ledger_never_records_unchecked_fields(tmp_path):
    secret = "pass" + "word=never-print-7391"
    doc.ledger_line(tmp_path, secret, "example.com", "/x?" + secret, 1, secret)
    text = doc._ledger_path(tmp_path).read_text()
    assert secret not in text and len(text.strip().split("\t")) == 6


def test_review_stream_creation_and_write_failure_are_private_and_clean(tmp_path, monkeypatch):
    secret = "pass" + "word=never-print-7391"
    getter = Getter({("example.com", "/x"): html_page("<p>public guide</p>")})
    original = os.fdopen
    class Broken:
        def __init__(self, fd):
            self.fd = fd
        def __enter__(self):
            return self
        def __exit__(self, *args):
            os.close(self.fd)
        def write(self, data):
            os.write(self.fd, data[:2])
            raise OSError(secret)
    def fdopen(fd, mode="r", *a, **kw):
        return Broken(fd) if mode == "wb" else original(fd, mode, *a, **kw)
    monkeypatch.setattr(os, "fdopen", fdopen)
    answer, stream = doc.serve({"url": "https://example.com/x"}, PROJECT, doc.Limiter(lambda: None), None,
                               getter=getter, hosts=HOSTS, tmp_dir=tmp_path)
    assert not answer["ok"] and stream is None and secret not in answer["error"]
    assert list(tmp_path.iterdir()) == []
    answer, stream = doc.serve({"url": "https://example.com/x"}, PROJECT, doc.Limiter(lambda: None), None,
                               getter=getter, hosts=HOSTS, tmp_dir=tmp_path / "missing")
    assert not answer["ok"] and stream is None


def test_review_output_parent_swap_cannot_escape_project(tmp_path, project_folder, monkeypatch, capsys):
    parent = project_folder / "notes"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    def swap(_):
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
    _review_answer(monkeypatch, action=swap)
    assert _review_fetch("notes/guide.md") == 2
    assert list(outside.iterdir()) == []
    assert "outside" not in capsys.readouterr().err


def test_review_output_file_swap_cannot_overwrite_link_target(tmp_path, project_folder, monkeypatch):
    target = tmp_path / "outside"
    target.write_text("unchanged")
    output = project_folder / "guide.md"
    _review_answer(monkeypatch, action=lambda _: output.symlink_to(target))
    assert _review_fetch("guide.md") == 2
    assert target.read_text() == "unchanged"


def test_review_output_refuses_special_and_hardlinked_files(tmp_path, project_folder, monkeypatch):
    _review_answer(monkeypatch)
    fifo = project_folder / "pipe"
    os.mkfifo(fifo)
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        assert _review_fetch("pipe") == 2
    finally:
        os.close(fd)
    target = tmp_path / "target"
    target.write_text("unchanged")
    os.link(target, project_folder / "hardlink")
    assert _review_fetch("hardlink") == 2 and target.read_text() == "unchanged"


def test_review_output_is_published_complete_and_private(project_folder, monkeypatch):
    output = project_folder / "guide.md"
    output.write_text("previous version")
    _review_answer(monkeypatch, body="new guide\n" * 10000)
    original = os.replace
    published = []
    def replace(source, dest, **kwargs):
        assert output.read_text() == "previous version"
        fd = os.open(source, os.O_RDONLY, dir_fd=kwargs.get("src_dir_fd"))
        with os.fdopen(fd, "r") as stream:
            snapshot = stream.read()
        assert snapshot.endswith("new guide\n" * 10000)
        published.append(snapshot)
        return original(source, dest, **kwargs)
    monkeypatch.setattr(os, "replace", replace)
    assert _review_fetch("guide.md") == 0
    assert published == [output.read_text()]
    assert output.stat().st_mode & 0o777 == 0o600
    assert not list(project_folder.glob(".awb-doc-*"))


def test_review_failed_publication_preserves_existing_file(project_folder, monkeypatch, capsys):
    output = project_folder / "guide.md"
    output.write_text("previous version")
    _review_answer(monkeypatch)
    secret = "pass" + "word=never-print-7391"
    def denied(*a, **kw):
        raise OSError(secret)
    monkeypatch.setattr(os, "replace", denied)
    assert _review_fetch("guide.md") == 2
    assert output.read_text() == "previous version"
    assert secret not in capsys.readouterr().err
    assert not list(project_folder.glob(".awb-doc-*"))


def test_review_client_rejects_malformed_answers_and_private_headers(project_folder, monkeypatch, capsys):
    for answer in ([], {"ok": "yes"}, {"ok": True, "url": ["invalid"]},
                   {"ok": True, "fetched": "pass" + "word=never-print-7391"}):
        monkeypatch.setattr(keys, "request", lambda *a, **k: answer)
        assert _review_fetch() == 2
        captured = capsys.readouterr()
        assert not captured.out and "never-print" not in captured.err


def test_review_client_request_errors_never_echo_values(project_folder, monkeypatch, capsys):
    secret = "pass" + "word=never-print-7391"
    def request(*a, **kw):
        raise keys.KeysError(secret)
    monkeypatch.setattr(keys, "request", request)
    assert _review_fetch() == 2
    captured = capsys.readouterr()
    assert secret not in captured.err and not captured.out


def test_review_client_missing_and_linked_downloads_fail_cleanly(tmp_path, project_folder, monkeypatch, capsys):
    private = tmp_path / "private"
    private.write_text("pass" + "word=never-print-7391")
    def remove(download):
        download.unlink()
    def link(download):
        download.unlink()
        download.symlink_to(private)
    for action in (remove, link):
        _review_answer(monkeypatch, action=action)
        assert _review_fetch() == 2
        captured = capsys.readouterr()
        assert not captured.out and "never-print" not in captured.err
    assert private.read_text().endswith("never-print-7391")


def test_review_private_names_and_secrets_are_withheld_from_output(home, project_folder, monkeypatch, capsys):
    from tests import fixtures
    secret = "pass" + "word=never-print-7391"
    for private in (fixtures.CUSTOMER_FORMS[0], secret):
        _review_answer(monkeypatch, body=private)
        assert _review_fetch() == 1
        captured = capsys.readouterr()
        assert private not in captured.out + captured.err
        _review_answer(monkeypatch)
        assert _review_fetch(private + ".md") == 0
        captured = capsys.readouterr()
        assert private not in captured.out + captured.err


def test_review_private_service_errors_and_offlist_hosts_are_withheld(home, project_folder, monkeypatch, capsys):
    from tests import fixtures
    secret = "pass" + "word=never-print-7391"
    for private in (fixtures.CUSTOMER_FORMS[0], secret):
        monkeypatch.setattr(keys, "request", lambda *a, **k: {"ok": False, "error": "refused: " + private})
        assert _review_fetch() == 1
        captured = capsys.readouterr()
        assert private not in captured.out + captured.err
    assert doc.main(["fetch", "https://" + fixtures.CUSTOMER_DOMAIN + "/x"]) == 1
    assert fixtures.CUSTOMER_DOMAIN not in capsys.readouterr().err


def test_review_private_paths_do_not_reach_network_or_ledger(home, tmp_path):
    from tests import fixtures
    getter = Getter({})
    for value in (fixtures.CUSTOMER_FORMS[1], "pass" + "word=never-print-7391"):
        url = "https://example.com/" + value
        answer, stream = doc.serve({"url": url}, PROJECT, doc.Limiter(lambda: None), tmp_path,
                                   getter=getter, hosts=HOSTS)
        assert not answer["ok"] and stream is None
        assert value not in answer["error"] + doc._ledger_path(tmp_path).read_text()
    assert getter.asked == []


def test_review_service_withholds_private_page_text(home, tmp_path):
    from tests import fixtures
    for private in (fixtures.CUSTOMER_FORMS[0], "pass" + "word=never-print-7391"):
        answer, stream = doc.serve({"url": "https://example.com/x"}, PROJECT, doc.Limiter(lambda: None), None,
                                   getter=Getter({("example.com", "/x"): html_page("<p>" + private + "</p>")}),
                                   hosts=HOSTS, tmp_dir=tmp_path)
        if stream:
            stream.unlink()
        assert not answer["ok"] and stream is None and private not in answer["error"]


def test_review_host_add_withholds_private_notes(home, tmp_path, monkeypatch):
    from awb import config
    from tests import fixtures
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    path = tmp_path / "hosts"
    for private in (fixtures.CUSTOMER_FORMS[0], "pass" + "word=never-print-7391"):
        with pytest.raises(doc.DocError) as error:
            doc.add_host("docs.vendor-q7m4.example", private, path=path)
        assert private not in str(error.value)
    assert not path.exists()


def test_review_huge_help_chunk_counts_are_bounded():
    replies = iter([{"url": "https://example.com/x", "help_root": "https://example.com/"},
                    {"text": '<WebHelpSystem Toc="Data/Main.js"/>'},
                    {"text": "numchunks:" + "9" * 5000 + ",prefix:'chunk'"}])
    asked = []
    def fetcher(url, **kwargs):
        asked.append(url)
        return next(replies, {"text": ""})
    result = doc.find("https://example.com/x", "guide", fetcher=fetcher)
    assert result["count"] == 0 and len(asked) == 3 + doc.MAX_CHUNKS


def test_review_pdf_process_output_is_bounded_before_it_finishes(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(doc, "PDF_TEXT_MAX", 128)
    for channel in ("stdout", "stderr"):
        marker = tmp_path / (channel + ".done")
        reader = tmp_path / (channel + "-reader")
        reader.write_text("#!" + sys.executable + "\nimport sys\nfrom pathlib import Path\n"
                          "sys." + channel + ".buffer.write(b'x' * (1024 * 1024))\n"
                          "sys." + channel + ".flush()\nPath(" + repr(str(marker)) + ").touch()\n")
        reader.chmod(0o700)
        with pytest.raises(doc.DocError, match="larger than"):
            doc.pdf_text(b"%PDF-1.7", tool=str(reader), timeout=5)
        assert not marker.exists()


def test_review_control_characters_in_service_errors_are_withheld(project_folder, monkeypatch, capsys):
    for control in ("\x1b", "\x85", "\u202e"):
        monkeypatch.setattr(keys, "request", lambda *a, **k: {"ok": False, "error": "refused: " + control + "value"})
        assert _review_fetch() == 1
        assert control not in capsys.readouterr().err


def test_review_failed_name_check_withholds_values(home, project_folder, monkeypatch, capsys):
    from awb import check
    secret = "pass" + "word=never-print-7391"
    _review_answer(monkeypatch, body="unchecked body")
    def unavailable(*a, **kw):
        raise check.CheckUnavailable(secret)
    monkeypatch.setattr(check, "check_text", unavailable)
    assert _review_fetch() == 1
    captured = capsys.readouterr()
    assert not captured.out and secret not in captured.err


def test_review_hosts_command_withholds_registered_domains(home, tmp_path, monkeypatch, capsys):
    from tests import fixtures
    path = tmp_path / "hosts"
    path.write_text(fixtures.CUSTOMER_DOMAIN + "\nexample.com\n")
    monkeypatch.setattr(doc, "HOSTS_FILE", path)
    assert doc.main(["hosts"]) == 0
    output = capsys.readouterr().out
    assert fixtures.CUSTOMER_DOMAIN not in output and "example.com" in output


def test_review_client_temp_creation_failure_is_private(project_folder, monkeypatch, capsys):
    secret = "pass" + "word=never-print-7391"
    def denied(*a, **kw):
        raise OSError(secret)
    monkeypatch.setattr(tempfile, "mkstemp", denied)
    assert _review_fetch() == 2
    captured = capsys.readouterr()
    assert not captured.out and secret not in captured.err


def test_review_client_rejects_invalid_pdf_page_counts(project_folder, monkeypatch, capsys):
    for pages in (True, -1, "private", 10 ** 5000):
        _review_answer(monkeypatch, kind="pdf", pages=pages)
        assert _review_fetch() == 2
        assert not capsys.readouterr().out


def test_review_failed_http_setup_closes_the_connection(monkeypatch):
    import http.client
    for failed_step in ("request", "response"):
        closed = []
        class Connection:
            def __init__(self, *a, **kw):
                pass
            def request(self, *a, **kw):
                if failed_step == "request":
                    raise OSError("connection failed")
            def getresponse(self):
                raise OSError("response failed")
            def close(self):
                closed.append(True)
        monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
        with pytest.raises(OSError):
            doc.https_get("example.com", "/x")
        assert closed == [True]


def test_review_multiline_service_errors_cannot_forge_diagnostics(project_folder, monkeypatch, capsys):
    monkeypatch.setattr(keys, "request", lambda *a, **k: {"ok": False, "error": "refused: no\nforged status"})
    assert _review_fetch() == 1
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1 and "forged status" not in captured.err and not captured.out


def test_review_output_filenames_cannot_forge_status_lines(project_folder, monkeypatch, capsys):
    _review_answer(monkeypatch)
    assert _review_fetch("guide\nforged status.md") == 0
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1 and "forged status" not in captured.out


def _review_partial_write(monkeypatch, path):
    original = os.write
    target = path.stat().st_ino
    def write(fd, data):
        if os.fstat(fd).st_ino == target:
            original(fd, data[:3])
            raise OSError("disk full")
        return original(fd, data)
    monkeypatch.setattr(os, "write", write)


def test_review_failed_host_append_rolls_back_partial_row(tmp_path, monkeypatch):
    from awb import config
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    path = tmp_path / "hosts"
    path.write_text("example.com\n")
    _review_partial_write(monkeypatch, path)
    with pytest.raises(doc.DocError):
        doc.add_host("docs.vendor-q7m4.example", path=path)
    assert path.read_text() == "example.com\n"


def test_review_failed_ledger_append_rolls_back_partial_row(tmp_path, monkeypatch):
    path = doc._ledger_path(tmp_path)
    doc.ledger_line(tmp_path, PROJECT, "example.com", "/x", 1, "200")
    before = path.read_bytes()
    _review_partial_write(monkeypatch, path)
    doc.ledger_line(tmp_path, PROJECT, "example.com", "/x", 1, "200")
    assert path.read_bytes() == before
