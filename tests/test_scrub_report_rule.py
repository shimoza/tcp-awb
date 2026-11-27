"""His standing rule of 2026-11-27: a project session reports under- or over-scrub of its input itself with
`awb dev ask` (kind and file id, never the value). The template carries it on the inbox line and `awb projects sync`
appends it there once."""
from __future__ import annotations

from awb import projects, rulesync
from tests import fixtures as fx

CODE = "tcp-q7m4"
INBOX = ("- Files from him come through the bucket inboxes: when he says a file is in the inbox, run `awb inbox take "
         "<his words>`, never look in the vault. Copies of his own `awb import` wait in the outbox (the prompt says "
         "\"new input\" when they come): move them into input/.")


def test_the_template_ends_the_inbox_line_with_the_rule():
    text = projects._claude(CODE, "project", fx.CUSTOMER_CODE)
    line = [l for l in text.splitlines() if l.startswith("- Files from him come through the bucket inboxes")][0]
    assert line.endswith(projects.SCRUB_REPORT_RULE) and "`awb inbox take --outbox`" in line


def test_sync_brings_an_old_inbox_line_up_to_date_once(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# p\n\n" + INBOX + "\n- Never open an original or the vault.\n")
    (tmp_path / "STATE.md").write_text("Status: x\nNext: y\n")
    assert "CLAUDE.md" in rulesync.sync_project(tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text()
    assert text.count(projects.SCRUB_REPORT_RULE) == 1 and "awb inbox take --outbox" in text
    assert rulesync.sync_project(tmp_path) == []
