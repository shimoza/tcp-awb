"""Ask 2026-11-27-tcp-j5sy-2: the key service follows a redirect to a URL with a query (B-082) and names it as the
source; the client takes that source and still refuses a broken one."""
from __future__ import annotations

from awb.tcp import doc, keys
from tests.test_doc import project_folder  # noqa: F401  (a fixture)


def _answer(monkeypatch, url):
    def request(*args, download, **kwargs):
        download.write_text("public guide\n", encoding="utf-8")
        return {"ok": True, "url": url, "fetched": "2026-11-27 16:00 UTC", "kind": "text"}
    monkeypatch.setattr(keys, "request", request)


def test_a_source_with_the_query_of_a_redirect_is_taken(project_folder, monkeypatch, capsys):
    _answer(monkeypatch, "https://learn.microsoft.com/en-us/sql/x?view=sql-server-ver17")
    assert doc.main(["fetch", "https://learn.microsoft.com/en-us/sql/x"]) == 0
    assert "https://learn.microsoft.com/en-us/sql/x?view=sql-server-ver17" in capsys.readouterr().out


def test_a_source_with_a_query_that_is_not_url_safe_is_still_refused(project_folder, monkeypatch, capsys):
    _answer(monkeypatch, "https://learn.microsoft.com/x?a=<b>")
    assert doc.main(["fetch", "https://learn.microsoft.com/x"]) == 2
    assert "invalid source metadata" in capsys.readouterr().err
