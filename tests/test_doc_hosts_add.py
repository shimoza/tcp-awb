"""UX8 of 2026-11-11: `awb doc hosts add` allows a documentation host without a deploy.

The key service reads a second list beside rules/allow-domains.txt at each fetch: the file `doc_hosts` of the host
file names. The host file here is a temporary one that AWB_CONF points at; every host is invented.
"""
from __future__ import annotations

import stat

import pytest

from awb import cli, config, ownerwait
from awb.tcp import doc
from tests.test_doc import Getter, html_page

HOST = "kb.vendor-q7m4.example"
PROJECT = "tcp-ab2c"


@pytest.fixture
def extra(tmp_path, monkeypatch):
    """A host file with the line `doc_hosts` and the owner-side file it names (not there yet)."""
    f = tmp_path / "owner" / "doc-hosts.txt"
    f.parent.mkdir()
    conf = tmp_path / "paths.conf"
    conf.write_text("doc_hosts = %s\n" % f, encoding="utf-8")
    monkeypatch.setenv("AWB_CONF", str(conf))
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(doc, "_resolves", lambda host: False)
    return f


def serve(url: str, getter):
    req = {"op": "doc_get", "url": url, "project": PROJECT}
    answer, _ = doc.serve(req, PROJECT, doc.Limiter(lambda: None), None, getter=getter)
    return answer


def test_a_host_added_is_fetched_at_once_and_removing_the_line_refuses_it_again(extra, tmp_path, monkeypatch):
    monkeypatch.setattr(doc, "ledger_line", lambda *a, **k: None)
    url = "https://%s/guide" % HOST
    with pytest.raises(doc.DocError, match="not on rules/allow-domains.txt"):
        doc.fetch(url, getter=Getter({}))
    assert cli.main(["doc", "hosts", "add", HOST, "--why", "vendor guide"]) == 0
    page = doc.fetch(url, getter=Getter({(HOST, "/guide"): html_page()}))
    assert "Credential guard" in page["text"]
    extra.write_text("", encoding="utf-8")
    with pytest.raises(doc.DocError, match="not on rules/allow-domains.txt"):
        doc.fetch(url, getter=Getter({(HOST, "/guide"): html_page()}))


def test_the_service_side_serve_reads_the_file_at_each_call(extra, tmp_path, monkeypatch):
    monkeypatch.setattr(doc, "ledger_line", lambda *a, **k: None)
    url = "https://%s/guide" % HOST
    g = Getter({(HOST, "/guide"): html_page()})
    assert not serve(url, g)["ok"]
    doc.add_host(HOST)
    assert serve(url, g)["ok"]


def test_the_file_is_the_owners_and_the_work_user_cannot_write_it(extra, monkeypatch, capsys):
    doc.add_host(HOST, "vendor guide")
    mode = stat.S_IMODE(extra.stat().st_mode)
    assert mode & stat.S_IRUSR and mode & stat.S_IROTH and not mode & (stat.S_IWGRP | stat.S_IWOTH)
    assert extra.read_text(encoding="utf-8").split()[0] == HOST
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert cli.main(["doc", "hosts", "add", "other.vendor-q7m4.example"]) == 1
    assert "work user cannot" in capsys.readouterr().err
    assert doc.extra_hosts(extra) == [HOST]


def test_the_file_is_written_only_when_the_host_file_names_it(tmp_path, monkeypatch, capsys):
    conf = tmp_path / "paths.conf"
    conf.write_text("owner = x\n", encoding="utf-8")
    monkeypatch.setenv("AWB_CONF", str(conf))
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert cli.main(["doc", "hosts", "add", HOST]) == 1
    assert "doc_hosts" in capsys.readouterr().err


@pytest.mark.parametrize("bad, word", [
    ("*.vendor-q7m4.example", "wildcard"),
    ("192.0.2.7", "address"),
    ("2001:db8::1", "address"),
    ("iam.eu-de.otc.t-systems.com", "TCP API"),
    ("http://%s" % HOST, "https only"),
    ("%s/path" % HOST, "plain host"),
    ("example.com", "already"),
])
def test_a_wildcard_an_address_a_tcp_api_host_and_the_like_are_refused(extra, bad, word, capsys):
    assert cli.main(["doc", "hosts", "add", bad]) == 1
    assert word in capsys.readouterr().err
    assert doc.extra_hosts(extra) == []


def test_a_portal_host_of_the_vendor_list_is_refused(extra, tmp_path, monkeypatch, capsys):
    from awb import vendorhosts

    monkeypatch.setattr(vendorhosts, "read", lambda path=None: ([], ["portal.vendor-q7m4.example"]))
    assert cli.main(["doc", "hosts", "add", "portal.vendor-q7m4.example"]) == 1
    assert "portal" in capsys.readouterr().err
    assert doc.extra_hosts(extra) == []


def test_the_www_and_docs_hosts_of_the_domain_are_offered_when_they_resolve(extra, monkeypatch, capsys):
    monkeypatch.setattr(doc, "_resolves", lambda host: host in ("www.vendor-q7m4.example", "vendor-q7m4.example"))
    assert cli.main(["doc", "hosts", "add", "vendor-q7m4.example"]) == 0
    out = capsys.readouterr().out
    assert "www.vendor-q7m4.example" in out and "docs.vendor-q7m4.example" not in out
    assert doc.extra_hosts(extra) == ["vendor-q7m4.example"]


def test_awb_doc_hosts_marks_the_source_of_a_host(extra, capsys):
    doc.add_host(HOST)
    assert cli.main(["doc", "hosts"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert HOST + "  (added on the host, not in the repository)" in lines
    assert "learn.microsoft.com" in lines


def test_owner_status_counts_the_hosts_added_on_the_host(extra):
    assert ownerwait.doc_host_lines() == []
    doc.add_host(HOST)
    assert ownerwait.doc_host_lines() == ["1 host added on the host, not in the repository"]
    doc.add_host("docs." + HOST)
    assert ownerwait.doc_host_lines() == ["2 hosts added on the host, not in the repository"]
    assert ownerwait.doc_host_lines()[0] in ownerwait.notes(path=extra.parent / "none.json")
