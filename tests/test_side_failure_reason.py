"""Ask 2026-11-25-tcp-s2z5-1: a failed side session shows why in awb side status: the client writes its error into
the JSON answer, not to standard error."""
import json

from awb import side
import pytest

from tests.test_side import _record, proj  # noqa: F401 - the fixture


@pytest.fixture(autouse=True)
def side_register(home):
    """The output checks use the temporary fixture register, never a host service."""
    return home.register


def test_a_failed_session_shows_the_error_of_the_client_answer(proj):
    p = _record(proj)
    p["exit"].write_text("1")
    p["out"].write_text(json.dumps({"is_error": True, "subtype": "error", "num_turns": 0,
                                    "result": "There's an issue with the selected model.\nmore"}))
    lines = side.status_lines(proj)
    assert " failed " in lines[0]
    assert lines[1] == "  reason: There's an issue with the selected model."


def test_a_failed_session_with_nothing_says_so(proj):
    p = _record(proj)
    p["exit"].write_text("1")
    lines = side.status_lines(proj)
    assert lines[1] == "  reason: the client gave no reason (no answer and an empty error file)"
