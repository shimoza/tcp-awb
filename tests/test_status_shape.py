"""UX11: Status: and Next: stay short (awb/status.py shape): at most 300 characters each, no sentence twice."""
from __future__ import annotations

from awb import status
from awb.tcp import board
from tests.test_status import STATE_TEXT, repo  # noqa: F401  (fixture of the status tests)


def test_301_characters_held_and_300_pass():
    assert status.shape("a" * 300, "The boot test.") is None
    assert "301 characters" in status.shape("The image is imported.", "b" * 301)


def test_a_repeated_sentence_is_held():
    assert status.shape("The boot test runs. The image is imported.", "Then the boot test runs.") is None
    assert "occur twice" in status.shape("The boot test runs.", "The image is in. The boot test runs.")


def test_the_hold_message_names_the_counts(repo):  # noqa: F811
    text = "# State of tcp-q7m4\n\nStatus: %s\nNext: %s\n" % ("Done. " * 2 + "x" * 300, "Done.")
    (repo / "STATE.md").write_text(text, encoding="utf-8")
    msg = status.reminder(repo)
    assert msg and "Status: has" in msg and "characters, at most 300" in msg and "occur twice" in msg
    assert "move the detail below the two lines" in msg


def test_the_board_cell_is_cut_at_300_characters():
    p = {"code": "tcp-q7m4", "kind": "tcp", "customer": "CUST-Q7M4", "goal": "g", "created": "2026-09-22",
         "status": {"summary": "Short.", "next": "n" * 1200, "updated": None, "behind": 0},
         "open_items": 0, "live_resources": 0, "waiting_on": {"items": [], "total": 0},
         "open": {"items": [], "total": 0}, "deliverables": {"status": "available", "counts": {}}}
    out = board.markdown({"summary": {"projects": 1, "current": 1, "lagging": 0, "without_status": 0},
                          "generated_at": None, "projects": [p], "parked": []})
    row = next(x for x in out.splitlines() if x.startswith("| tcp-q7m4"))
    assert max(len(c.strip()) for c in row.split(" | ")) <= 303 and "n" * 300 + "..." in row
