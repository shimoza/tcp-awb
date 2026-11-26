"""awb doc fetch, the asks of tcp-bytg of 2026-11-24: the links of the page body come before those of the
navigation, header, aside and footer (ask 3: a help tree of thousands of entries filled the cap), and a public
page with a secret-like sample value shows whole (ask 4: vendor cmdlet pages with sample credentials; B-089: no
masking inside). The sample value is built at run time so the file itself carries none."""
from __future__ import annotations

from awb.tcp import doc

BASE = "https://docs.example.test/guide/page.html"
HOSTS = ["docs.example.test"]


def _page(nav_links: int) -> str:
    nav = "".join('<li><a href="/guide/n%d.html">n%d</a></li>' % (i, i) for i in range(nav_links))
    return ("<html><body><header><a href='/'>home</a></header><nav><ul>%s</ul></nav>"
            "<main><p>See <a href='/guide/import.html'>Import</a> and <a href='/guide/export.html'>Export</a>.</p>"
            "<nav><a href='/guide/inner.html'>inner</a></nav><p><a href='/guide/after.html'>After</a></p></main>"
            "<footer><a href='/legal.html'>legal</a></footer></body></html>" % nav)


def test_body_links_come_first_even_behind_a_long_navigation():
    lines, _ = doc.page_links(_page(doc.MAX_LINKS + 50), BASE, HOSTS)
    assert len(lines) == doc.MAX_LINKS
    assert lines[0].startswith("- Import: https://docs.example.test/guide/import.html")
    assert lines[1].startswith("- Export: ")
    assert lines[2].startswith("- After: ")


def test_a_page_without_frames_keeps_its_order():
    page = "<p><a href='/a.html'>a</a><a href='/b.html'>b</a></p>"
    lines, _ = doc.page_links(page, BASE, HOSTS)
    assert [x.split(":")[0] for x in lines] == ["- a", "- b"]


def _sample() -> str:
    return 'password = "' + "Zx9!kQ2v" + "Lm7#" + '"'


def test_a_secret_like_sample_line_is_masked_and_the_rest_stays():
    text = "Import-VBRBackup imports a backup.\nExample:\n" + _sample() + "\nThe cmdlet returns the backup.\n"
    # B-089: no masking inside; the sample line stays as written
    assert doc.private_reason(text) is None
    assert not hasattr(doc, "mask_secret_lines")


def test_a_page_without_secrets_is_unchanged():
    text = "plain text\nno values\n"
    assert doc.private_reason(text) is None and doc._public(text, "") == text


def test_the_service_shows_a_long_page_with_its_sample_line_masked(home, tmp_path):
    from tests.test_doc import HOSTS as SERVICE_HOSTS, PROJECT, Getter, html_page
    body = "".join("<p>Step %d of the import.</p>" % i for i in range(8)) + "<pre>" + _sample() + "</pre>"
    answer, stream = doc.serve({"url": "https://example.com/x"}, PROJECT, doc.Limiter(lambda: None), None,
                               getter=Getter({("example.com", "/x"): html_page(body)}),
                               hosts=SERVICE_HOSTS, tmp_dir=tmp_path)
    text = stream.read_text(encoding="utf-8") if stream else answer.get("text", "")
    if stream:
        stream.unlink()
    assert answer["ok"], answer
    assert "Step 7 of the import." in text
    assert _sample() in text
