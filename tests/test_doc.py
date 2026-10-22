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


@pytest.mark.parametrize("ctype", ["application/pdf", "image/png", "application/octet-stream", "",
                                   "application/zip", "text/javascript"])
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
