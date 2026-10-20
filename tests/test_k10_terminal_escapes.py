"""K10: control and format characters of OPEN.md and RESOURCES.md never reach the owner's terminal."""
from __future__ import annotations

from awb import projects

ESC = "\x1b"


def test_open_items_drop_escapes_and_format_characters(tmp_path):
    (tmp_path / "OPEN.md").write_text("- owner: %s]52;c;QUJD\x07 check ‮ the plan\n- plain\x85item\n" % ESC,
                                      encoding="utf-8")
    text = "\n".join(projects.open_item_lines(tmp_path))
    assert ESC not in text and "\x07" not in text and "‮" not in text and "\x85" not in text
    assert "check the plan" in text.replace("  ", " ")


def test_owner_items_drop_escapes():
    items = projects._owner_items("## Owner\n\n- do %s[2J this\n" % ESC)
    assert items == ["do [2J this"]


def test_live_resource_lines_drop_escapes(tmp_path):
    (tmp_path / "RESOURCES.md").write_text(
        "| handle | type | state |\n|---|---|---|\n| vm-%s]0;x | ecs | live |\n" % ESC, encoding="utf-8")
    lines = projects.live_resource_lines(tmp_path)
    assert len(lines) == 1 and ESC not in lines[0]
