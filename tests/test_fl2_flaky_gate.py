"""FL2 (2026-10-30): two tests of tests/test_gt_put_no_gate.py failed now and then under a full run and passed alone.
The cause was the random values, not the load:

- a value under a key that names a secret was a secret only above 3.5 bits per character; a random value of 25
  characters falls under that about once in 35 and the commit gate passed it;
- `awb paste --own` checked the names in the text as given: a short form of a name inside a random token (the
  register holds a form of three letters) refused the paste about once in 350, although the token is shortened
  or replaced before anything is written.

Every value is made at run time, so that no test file holds a secret. The only names are those of tests/fixtures.py.
"""
from __future__ import annotations

from awb import gate, labsecrets, paste
from tests import fixtures as fx
from tests.test_hooks import project  # noqa: F401 (fixture)

ACRONYM = next(f for f in fx.CUSTOMER_FORMS if len(f) == 3)


def keyed(value: str) -> str:
    key = "_".join(("db", "pass" + "word"))
    return "The plan.\n%s = %s\nOne more line.\n" % (key, value)


def low_entropy_secret() -> str:
    """25 characters of the shape the GT test makes, with repeats that put it under 3.5 bits per character."""
    return "Qx" + "b29c8992999ccb68cca7" + "Zr7"


def test_fl2_a_keyed_secret_under_the_old_entropy_floor_is_still_a_secret():
    value = low_entropy_secret()
    assert gate._MIN_PASSWORD_ENTROPY <= gate._entropy(value) < gate._MIN_SECRET_ENTROPY, "control: the shape"
    assert gate._detect_secret(keyed(value)) != []
    tag = "pass" + "word"
    assert gate._detect_xml_secrets("<%s>%s</%s>" % (tag, value, tag)) != []


def test_fl2_keyed_prose_and_names_under_the_old_floor_stay_clean():
    key = "_".join(("db", "pass" + "word"))
    base32 = "JBSWY3DPEHPK3PXP" * 2
    for value in ('"enter the one you were sent"', "_detect_private_key", "SOME_SETTING_NAME_HERE",
                  "awb/tenant/test-1", base32):
        assert gate._detect_secret("%s = %s\n" % (key, value)) == [], value


def test_fl2_paste_own_takes_a_log_whose_shortened_token_carries_a_short_form(project):  # noqa: F811
    token = "MII" + "Ab" + ACRONYM.capitalize() + "Q7wE9rT2yU4iO6pA8sD0fG1hJ3kL5zX7cV9bN2mQ4wE6rT8y" * 2
    text = "X-Subject-Token: %s\nproject done\n" % token
    path, n = paste.own(text, project)
    now = path.read_text(encoding="utf-8")
    assert token not in now and token[:8] + "...(" in now and n >= 1


def test_fl2_paste_own_still_refuses_a_name_in_the_written_text_and_a_glued_one(project):  # noqa: F811
    import pytest

    with pytest.raises(paste.PasteRefused, match="registered name"):
        paste.own("The run for %s.\n" % fx.CUSTOMER_FORMS[1], project)
    glued = "%sabcdefghij...(3 chars)" % fx.CUSTOMER_FORMS[0].split()[0]
    assert fx.CUSTOMER_FORMS[0].split()[0] in labsecrets.without_short(glued)
