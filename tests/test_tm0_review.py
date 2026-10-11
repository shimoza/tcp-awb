"""The sealed-host fixes TM0 (33ba690..482c16c, b584f6d) against a crafted paste or a hostile project session, from
the attacker review of 2026-10-10; each part names its finding. The four tests of group g4 are kept with the prefix
dropped and the same asserts; the tests after each one are this suite's own.

Every name is an invented fixture name of tests/fixtures.py; no test prints a value.
"""
from __future__ import annotations

import pytest

from awb import gate, ledger
from tests import fixtures as fx


# --------------------------------------------------------------------------- finding 7: the possessive


def test_a_possessive_after_a_listed_first_name_still_meets_the_short_text_rule(tmp_path, home, register_path):
    """482c16c promises that the commit message gate and ledger.screen find "a run that opens with a listed first
    name". Attack: a crafted paste (or a plain commit subject) writes the person with the English possessive,
    `per <first> <last>'s notes`. The token `<last>'s` is no name word for wipe._name_word (the part after the
    apostrophe is lower case), so the run shrinks to the first name alone and nothing is found."""
    subject = "sizing per %s's notes" % fx.FIRST_LAST
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(subject + "\n", encoding="utf-8")
    found = gate.scan_message(msg, None)
    assert [f.cls for f in found] == ["candidate-person"], "the commit gate let the possessive form pass"
    with pytest.raises(ledger.Refused):
        ledger.add("other", subject, day="2026-09-22", p=home)


@pytest.mark.parametrize("mark", ["’s", "’S", "'S"])
def test_a_possessive_with_a_typographic_apostrophe_still_meets_the_short_text_rule(tmp_path, home, register_path,
                                                                                    mark):
    """Finding 7: the typographic apostrophe of a word processor and a possessive in capitals are the same name."""
    last = fx.FIRST_LAST.split(" ")[1]
    subject = "sizing per %s %s%s notes" % (fx.LISTED_FIRST, last.upper() if mark.isupper() else last, mark)
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(subject + "\n", encoding="utf-8")
    assert [f.cls for f in gate.scan_message(msg, None)] == ["candidate-person"]
    with pytest.raises(ledger.Refused):
        ledger.add("other", subject, day="2026-09-22", p=home)


def test_a_possessive_of_a_plain_word_stays_no_name(tmp_path, home, register_path):
    """The possessive alone makes no name: a lower case word with 's and a product's possessive pass."""
    subject = "per the vendor's notes and the cluster's sizing"
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(subject + "\n", encoding="utf-8")
    assert gate.scan_message(msg, None) == []
    ledger.add("other", subject, day="2026-09-22", p=home)
