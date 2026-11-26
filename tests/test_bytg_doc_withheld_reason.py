"""awb doc fetch says why it refuses a page, never the value (ask 2026-11-22-tcp-bytg-1).

A public vendor page was refused with "private documentation text withheld" and the project could not tell which
word was taken as private. B-089: a page runs no content check inside; only a control character is refused, and the
refusal names its line.
"""
from __future__ import annotations

from awb import check
from awb.tcp import doc

PAGE = "first line\nsecond line\nthird line holds wordx here\nlast line\n"


def _hits(monkeypatch, hits):
    monkeypatch.setattr(check, "check_text", lambda text, register: hits)
    monkeypatch.setattr(check, "_secret_classes", lambda text: set())


def test_bytg_a_name_hit_is_named_by_class_and_line_never_by_value(monkeypatch):
    # B-089: a registered form of a page is shown as written
    _hits(monkeypatch, [{"start": PAGE.index("wordx"), "length": 5, "cls": "name"}])
    assert doc.private_reason(PAGE) is None
    assert doc._public(PAGE, "") == PAGE


def test_bytg_a_public_page_has_no_reason_and_stays_whole(monkeypatch):
    _hits(monkeypatch, [{"start": 0, "length": 3, "cls": "url"}])
    assert doc.private_reason(PAGE) is None
    assert doc._public(PAGE, "") == PAGE


def test_bytg_a_secret_is_named_by_its_class(monkeypatch):
    # B-089: a sample password of a vendor page is shown whole
    monkeypatch.setattr(check, "_secret_classes", lambda text: {"password"})
    assert doc.private_reason(PAGE) is None


def test_bytg_a_control_character_is_named_with_its_line():
    assert doc.private_reason("a\nb\x07c\n") == "a control character at line 2 of 3"
