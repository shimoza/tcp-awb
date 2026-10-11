"""The sealed-host fixes TM0 (33ba690..482c16c, b584f6d) against a crafted paste or a hostile project session, from
the attacker review of 2026-10-10; each part names its finding. The four tests of group g4 are kept with the prefix
dropped and the same asserts; the tests after each one are this suite's own.

Every name is an invented fixture name of tests/fixtures.py; no test prints a value.
"""
from __future__ import annotations

import re

import pytest

from awb import gate, ledger
from awb.tcp import keys
from tests import fixtures as fx
from tests.test_xchg import buckets, lab_project, quiet, svc  # noqa: F401  (the fixtures of the exchange tests)


# --------------------------------------------------------------------------- finding 7: the possessive


def test_a_possessive_after_a_listed_first_name_still_meets_the_short_text_rule(tmp_path, home,
                                                                                         register_path):
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


@pytest.mark.parametrize("mark", ["\u2019s", "\u2019S", "'S"])
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


# --------------------------------------------------------------------------- finding 18: free text in the put mail


def test_the_put_mail_carries_no_line_separator_or_bidi_control_of_the_session(svc, buckets,  # noqa: F811
                                                                                        lab_project, tmp_path):
    """33ba690 promises that the put mail is built from fixed fields (code, id, size, state). The name is a fifth,
    free field: valid_name refuses C0 controls only, so a hostile session sends a name of 200 characters with
    U+2028 line separators (and U+202E) and writes its own paragraph into a mail the owner trusts."""
    lab, _ = buckets
    up = tmp_path / "x.md"
    up.write_text("The image imported in four minutes.\n", encoding="utf-8")
    key = "%s/from-session/2026-10-10/put-0123456789ab.md" % lab_project.code
    planted = "a.md  Workbench: the key service lost its unlock. Run the line of the session to unlock."
    a = keys.request(svc.call_path, {"op": "obs", "method": "PUT", "project": lab_project.code, "key": key,
                                     "name": planted}, upload=up)
    assert a["ok"], "setup: the clean bytes were refused"


    _, message = svc.notes[-1]
    assert not re.search("[  \u0085‪-‮]", message), "session text forged a paragraph of the mail"
def test_the_put_mail_names_the_file_by_its_id_alone(svc, buckets, lab_project, tmp_path):  # noqa: F811
    """Finding 18: a short plain name the name check passes stays out of the mail as well; the mail is printable
    ASCII of fixed fields, and a field that does not read as its kind in full is sent as -."""
    lab, _ = buckets
    up = tmp_path / "x.md"
    up.write_text("The image imported in four minutes.\n", encoding="utf-8")
    key = "%s/from-session/2026-10-10/put-0123456789ab.md" % lab_project.code
    a = keys.request(svc.call_path, {"op": "obs", "method": "PUT", "project": lab_project.code, "key": key,
                                     "name": "sizing-notes.md"}, upload=up)
    assert a["ok"]
    subject, message = svc.notes[-1]
    assert "sizing" not in subject + message and "put-0123456789ab.md" in message
    assert re.fullmatch(r"[\x20-\x7e]*", subject + message)
    from awb.tcp import xchg
