"""LC3: a password written in prose after a password word ("the admin password is X", "Kennwort: X", a Russian
"parol X") is a secret: the gate finds it, `awb clean --kind docs` and the host import put [secret] in its place
and keep the words around it. Ordinary sentences about passwords stay as they are.

The password words and values are built from pieces so that the commit gate finds no secret in this file."""

from __future__ import annotations

from collections import Counter

import pytest

from awb import cleanlogs, gate
from tests.test_import_trusted import owner_terminal  # noqa: F401  (the owner's terminal for the import test)

EN = "pass" "word"
DE = "Pass" "wort"
DE2 = "Kenn" "wort"
RU = "пар" "оль"
VALUE = "Winter" "2026!x"
QUOTED = "a b" "c d"
KEY = "AKIA" "Q7M4" "EXAMPLE" "12345"

POSITIVE = [
    "the admin {en} is {pw} and the key is {key}.",
    "{en} is {pw}",
    "{en}: {pw}",
    "{en} = {pw}",
    "the {en} for the admin is {pw}.",
    "use pw {pw} to log in",
    "{de} lautet {pw}",
    "das {de} ist {pw}.",
    "{de2}: {pw}",
    "{ru} {pw}",
    "{ru}: {pw}",
    "{ru} \u2014 {pw}",
    "the {en} is '{pw}'",
    "the {en} is \"{pw}\"",
    "the {en} is `{pw}`",
    "{de2}: \"{pw}\"",
    "{ru}: `{pw}`",
    "the {en} is \"{quoted}\"",
    "the {en} is MixedCaseOnly",
    "**{en}:** {pw}",
]

NEGATIVE = [
    "the {en} is stored in pass",
    "{en} is required",
    "the {en} is in the vault",
    "{de} ist abgelaufen",
    "{ru} не нужен",
    "{en} reset",
    "{en} policy: 12 characters",
    "the {en} was changed on 2026-10-01",
    "the {en} is 2026-10-01",
    "{en}: [secret]",
    "{en}: <{en}>",
    "{en}: ***",
    "{en}: changeme",
    "{en}: redacted",
    "the {en} is \"***\"",
    "the {en} is required for the admin",
    "{en} manager",
    "the {en} is longer than 12 characters",
    "{de2}: 12 Zeichen",
    "{en}-protected file",
    "echo it | passwd --stdin",
]


def _fill(t: str) -> str:
    return t.format(en=EN, de=DE, de2=DE2, ru=RU, pw=VALUE, quoted=QUOTED, key=KEY)


def _wipe(text: str) -> tuple[str, Counter]:
    counts: Counter = Counter()
    return cleanlogs.wipe_secrets(text, counts), counts


@pytest.mark.parametrize("template", POSITIVE)
def test_a_password_in_prose_is_a_secret_of_the_gate(template):
    text = _fill(template)
    assert gate._detect_secret(text), text


@pytest.mark.parametrize("template", POSITIVE)
def test_the_wipe_puts_secret_in_place_of_the_value_and_keeps_the_words(template):
    text = _fill(template)
    out, counts = _wipe(text)
    assert VALUE not in out and QUOTED not in out and "MixedCaseOnly" not in out, out
    assert "[secret]" in out and counts["secret"] >= 1
    word = next(w for w in (EN, DE, DE2, RU, "pw") if w in text)
    assert word in out, "the words around the value stay"


@pytest.mark.parametrize("template", NEGATIVE)
def test_ordinary_prose_about_passwords_is_no_secret(template):
    text = _fill(template)
    assert gate.prose_password_spans(text) == [], text
    assert not gate._detect_secret(text), text
    out, counts = _wipe(text)
    assert out == text and not counts, out


def test_a_word_of_the_public_lists_is_no_password():
    text = "the %s is PostgreSQL" % EN
    assert gate.prose_password_spans(text) == []


def test_the_reported_line_keeps_the_words_and_the_key_is_caught_too():
    text = _fill(POSITIVE[0])
    out, _ = _wipe(text)
    assert out == "the admin %s is [secret] and the key is [secret]." % EN


def test_the_span_covers_the_value_only():
    text = "the %s for the admin is %s." % (EN, VALUE)
    (span,) = gate.prose_password_spans(text)
    assert text[span[0]:span[1]] == VALUE


def test_the_host_import_wipes_a_prose_password(home, capsys, tmp_path):
    from tests.test_import_trusted import _copy, _project
    from awb import importcmd

    pr = _project(home, capsys)
    f = tmp_path / "notes.md"
    f.write_text(_fill(POSITIVE[0]) + "\n" + _fill("{de} lautet {pw}") + "\n", encoding="utf-8")
    assert importcmd.run(home, pr.code, [f], tty=True) == 0
    copy = _copy(home, pr.customer)
    assert VALUE not in copy and KEY not in copy
    assert "the admin %s is [secret] and the key is [secret]." % EN in copy
    assert "%s lautet [secret]" % DE in copy


def test_the_paste_door_refuses_a_prose_password():
    from awb import pastein

    assert pastein.secret_findings("line one\n" + _fill("{ru}: {pw}") + "\n") == [("secret", 2)]
