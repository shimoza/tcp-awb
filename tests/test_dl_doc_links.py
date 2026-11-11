"""Task DL (2026-11-11): `awb doc fetch --links` and `awb doc find URL WORDS` over the index of a MadCap Flare help.

The help lives on an invented host (docs.example.org of the test list); every file is answered by the stub getter of
test_doc, so no socket leaves the process."""
from __future__ import annotations

import pytest

from awb import cli
from awb.tcp import doc, keys
from tests.test_doc import HOSTS, PROJECT, Getter, html_page, ledger_rows, project_folder, service  # noqa: F401

H = "docs.example.org"
ROOT = "/help/en/"
TOPIC = ROOT + "Content/fw/setup/vm_setup.html"
PAGE = ('<html data-mc-path-to-help-system="../../../" lang="en"><head><title>Set up the VM</title></head><body>'
        '<h1>Set up the VM</h1><p>See <a href="../ha/before.html#top">Before you begin</a>, '
        '<a href="../ha/before.html">again</a>, <a href="#">top</a>, <a href="mailto:x@example.org">mail</a>, '
        '<a href="https://portal.vendor.example/dl?x=1">Downloads</a> and '
        '<a href="https://example.com/notes">Release notes</a>.</p></body></html>')
SYSTEM = b'<?xml version="1.0"?><WebHelpSystem DefaultUrl="Content/home.html" Toc="Data/Tocs/Main_.js" />'
TOC = b"define({numchunks:2,prefix:'Main__Chunk',chunkstart:['/Content/a.html'],tree:{n:[{i:0,c:0}]}});"
CHUNK0 = (b"define({'/Content/fw/rba/users-roles.html':{i:[1],t:['manage users and roles on the device'],b:['']},"
          b"'/Content/fw/status/users_web.html':{i:[2],t:['Users and Roles'],b:['']},"
          b"'/Content/fw/setup/vm_setup.html':{i:[3],t:['Set up the VM'],b:['']}});")
CHUNK1 = b"define({'/Content/fw/rba/roles_wsm.html':{i:[4],t:['define roles with the server\\'s center'],b:['']}});"
JS = {"content-type": "application/javascript"}


def help_site(**over):
    pages = {(H, TOPIC): html_page(PAGE),
             (H, ROOT + "Data/HelpSystem.xml"): (200, {"content-type": "text/xml"}, SYSTEM),
             (H, ROOT + "Data/Tocs/Main_.js"): (200, JS, TOC),
             (H, ROOT + "Data/Tocs/Main__Chunk0.js"): (200, JS, CHUNK0),
             (H, ROOT + "Data/Tocs/Main__Chunk1.js"): (200, JS, CHUNK1),
             (H, "/plain.html"): html_page("<html><body><p>no help</p></body></html>")}
    pages.update(over)
    return Getter(pages)


def test_dl_links_are_absolute_once_each_and_a_host_off_the_list_is_marked():
    page = doc.fetch("https://%s%s" % (H, TOPIC), getter=help_site(), hosts=HOSTS, links=True)
    text = page["text"]
    links = text[text.index("## Links"):]
    assert links.startswith("## Links (3)")
    assert "- Before you begin: https://%s%sContent/fw/ha/before.html\n" % (H, ROOT) in links
    assert "again" not in links and "mailto" not in links and "top" not in links
    assert "- Downloads: https://portal.vendor.example/dl?x=1 (not an allowed host)" in links
    assert "- Release notes: https://example.com/notes\n" in links
    assert "MadCap Flare help system (root https://%s%s)" % (H, ROOT) in text and "awb doc find" in text
    assert page["help_root"] == "https://%s%s" % (H, ROOT)
    plain = doc.fetch("https://%s%s" % (H, TOPIC), getter=help_site(), hosts=HOSTS)
    assert "## Links" not in plain["text"]


def test_dl_find_reads_the_index_chain_and_lists_only_matching_titles(tmp_path):
    g = help_site()
    with service(tmp_path, g) as s:
        out = tmp_path / "found.txt"
        a = keys.request(s.call_path, {"op": "doc_get", "url": "https://%s%s" % (H, TOPIC), "project": PROJECT,
                                       "find": "users  ROLES"}, download=out)
        assert a["ok"] and a["kind"] == "find", a
        text = out.read_text(encoding="utf-8")
        assert text.startswith('# 2 pages of the help at https://%s%s match "users ROLES"' % (H, ROOT))
        assert "- manage users and roles on the device: https://%s%sContent/fw/rba/users-roles.html" % (H, ROOT) in text
        assert "- Users and Roles: https://%s%sContent/fw/status/users_web.html" % (H, ROOT) in text
        assert "vm_setup" not in text and "define(" not in text
        b = keys.request(s.call_path, {"op": "doc_get", "url": "https://%s%s" % (H, TOPIC), "project": PROJECT,
                                       "find": "server center"}, download=out)
        assert b["ok"] and "define roles with the server's center" in out.read_text(encoding="utf-8")
    assert [p for _h, p in g.asked[:5]] == [TOPIC, ROOT + "Data/HelpSystem.xml", ROOT + "Data/Tocs/Main_.js",
                                            ROOT + "Data/Tocs/Main__Chunk0.js", ROOT + "Data/Tocs/Main__Chunk1.js"]
    rows = ledger_rows(tmp_path)
    assert len(rows) == 10 and all(r[-1].strip() == "200" for r in rows)


def test_dl_find_refuses_a_page_without_an_index_bad_words_and_a_plain_fetch_still_takes_no_script(tmp_path):
    g = help_site()
    with service(tmp_path, g) as s:
        out = tmp_path / "x.txt"
        a = keys.request(s.call_path, {"op": "doc_get", "url": "https://%s/plain.html" % H, "project": PROJECT,
                                       "find": "users"}, download=out)
        assert not a["ok"] and "names no help index" in a["error"] and "--links" in a["error"]
        b = keys.request(s.call_path, {"op": "doc_get", "url": "https://%s%s" % (H, TOPIC), "project": PROJECT,
                                       "find": "x"}, download=out)
        assert not b["ok"] and "WORDS are 2 to 120" in b["error"]
        c = keys.request(s.call_path, {"op": "doc_get", "url": "https://%s%sData/Tocs/Main__Chunk0.js" % (H, ROOT),
                                       "project": PROJECT}, download=out)
        assert not c["ok"] and "not a text type" in c["error"]


def test_dl_every_file_of_a_find_takes_the_rate(tmp_path):
    g = help_site()
    with service(tmp_path, g) as s:
        s.doc_limiter.per_minute = 3
        a = keys.request(s.call_path, {"op": "doc_get", "url": "https://%s%s" % (H, TOPIC), "project": PROJECT,
                                       "find": "users"}, download=tmp_path / "x.txt")
        assert not a["ok"] and "a minute" in a["error"]
    assert len(g.asked) == 3


def test_dl_the_commands_print_links_and_found_pages(tmp_path, project_folder, monkeypatch, capsys):  # noqa: F811
    g = help_site()
    with service(tmp_path, g) as s:
        monkeypatch.setattr(keys, "call_socket", lambda: s.call_path)
        monkeypatch.setattr(doc, "allowed_hosts", lambda path=None: list(HOSTS))
        assert cli.main(["doc", "fetch", "https://%s%s" % (H, TOPIC), "--links"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("source: https://%s%s" % (H, TOPIC)) and "## Links (3)" in out
        assert cli.main(["doc", "find", "https://%s%s" % (H, TOPIC), "users", "roles"]) == 0
        out = capsys.readouterr().out
        assert "# 2 pages of the help" in out and "users-roles.html" in out
        assert cli.main(["doc", "find", "https://%s%s" % (H, TOPIC), "users", "--out", "notes/found.md"]) == 0
        assert "users_web.html" in (project_folder / "notes" / "found.md").read_text(encoding="utf-8")


def test_dl_the_reader_asks_for_any_type_last_so_an_index_host_answers():
    # the help host of the field answered 500 to an Accept without */* (2026-11-11)
    import inspect
    assert "*/*;q=0.1" in inspect.getsource(doc.https_get)
