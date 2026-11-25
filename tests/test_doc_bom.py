"""Ask 2026-11-25-tcp-s2z5-2: a public vendor page with a byte order mark or a zero-width mark is not withheld for a
control character; a real control character still is."""
from awb.tcp import doc


def test_a_byte_order_mark_in_a_page_is_dropped_and_the_page_is_public():
    page = "<html><body><h1>Extract utility</h1>\n﻿<p>Run the utility with the backup file.</p>\n" \
           "<p>Zero​width and soft­hyphen.</p></body></html>"
    text = doc._plain(doc.html_text(page))
    assert "﻿" not in text and "​" not in text and "­" not in text
    assert doc.private_reason(text) is None


def test_a_bell_character_is_still_withheld():
    assert doc.private_reason("line one\nline\x07 two\n") is not None
