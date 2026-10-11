"""The sealed-host fixes TM0 (33ba690..482c16c, b584f6d) against a crafted paste or a hostile project session, from
the attacker review of 2026-10-10; each part names its finding. The four tests of group g4 are kept with the prefix
dropped and the same asserts; the tests after each one are this suite's own.

Every name is an invented fixture name of tests/fixtures.py; no test prints a value.
"""
from __future__ import annotations

import re
import unicodedata

import pytest

from awb import check, confusables, confusables_latin, gate, kb, ledger, matcher
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


# --------------------------------------------------------------------------- finding 17: lookalike letters


def test_a_registered_name_in_lisu_capitals_never_reaches_the_put_mail(svc, buckets, lab_project,  # noqa: F811
                                                                                 tmp_path):
    """33ba690 promises that the name of a put travels into the owner's mail only after the service's name check,
    and the matcher promises that letters of other scripts that look like Latin letters count as those letters.
    Attack: a hostile project session sends a raw put on the call socket whose name writes the registered short
    form with a Lisu capital (U+A4DC looks like Z, U+A4F3 like O), which the lookalike table does not list. The
    check passes it, so the mail shows the customer's name to the owner's mail provider; the same spelling passes
    kb.screen, ledger.screen and the commit gate."""
    lab, _ = buckets
    form = fx.CUSTOMER_FORMS[1]
    lisu = "ꓜ" + form[1:-1].upper() + "ꓳ"          # reads as the short form in capitals
    up = tmp_path / "x.md"
    up.write_text("The image imported in four minutes.\n", encoding="utf-8")
    key = "%s/from-session/2026-10-10/put-0123456789ab.md" % lab_project.code
    planted = "offer %s.md" % lisu
    a = keys.request(svc.call_path, {"op": "obs", "method": "PUT", "project": lab_project.code, "key": key,
                                     "name": planted}, upload=up)
    assert a["ok"], "setup: the clean bytes were refused"
    _, message = svc.notes[-1]
    assert lisu not in message, "a registered name in lookalike letters reached the put mail"


def _lookalike(word: str, script: str) -> str:
    """`word` with every letter that has a lookalike of `script` in the generated table written in that script."""
    by_letter: dict[str, str] = {}
    for c, latin in sorted(confusables_latin.LATIN.items()):
        if unicodedata.name(c, "").startswith(script) and latin not in by_letter:
            by_letter[latin] = c
    return "".join(by_letter.get(c, c) for c in word)


@pytest.mark.parametrize("script, shape", [("LISU", str.upper), ("CHEROKEE SMALL", str.lower),
                                           ("CHEROKEE LETTER", str.upper)])
def test_a_registered_name_in_lookalike_letters_or_mixed_scripts_is_a_hit_of_every_check(tmp_path, home,
                                                                                         register_path, script, shape):
    """Finding 17: the short form written in the lookalikes of one script, Latin letters left where the script has
    none (a mixed-script word: the Cherokee cases), is a name hit of the name check, the commit gate and the
    ledger."""
    form = shape(fx.CUSTOMER_FORMS[1])
    word = _lookalike(form, script)
    assert word != form and not word.isascii(), "setup: the script has no lookalike of the form"
    text = "the offer of %s is ready" % word
    assert [h["cls"] for h in check.check_text(text, register_path)] == ["name"]
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(text + "\n", encoding="utf-8")
    assert [f.cls for f in gate.scan_message(msg, gate._load_matcher(register_path))] == ["name"]
    with pytest.raises(ledger.Refused):
        ledger.add("other", text, day="2026-09-22", p=home)


def test_the_generated_table_maps_letters_of_other_scripts_to_one_latin_letter():
    """Finding 17: the table of awb/confusables_latin.py, generated from the UTS #39 data: no ASCII key, no digit,
    no mark, every value one Latin letter, the scripts of the review included, and the matcher's own fold kept."""
    table = confusables_latin.LATIN
    assert len(table) > 300 and confusables_latin.SOURCE.startswith("confusables.txt ")
    for c, latin in table.items():
        assert len(c) == 1 and not c.isascii() and unicodedata.digit(c, None) is None
        assert not unicodedata.category(c).startswith("M")
        assert len(latin) == 1 and latin.isascii() and latin.isalpha()
        assert c not in matcher._LOOKALIKES and c not in matcher._LIGATURES
    assert (table["ꓜ"], table["ꓳ"], table["ꮓ"], table["ꮃ"]) == ("Z", "O", "z", "w")
    assert table["ꓲ"] == "I", "a stroke that is no lower case letter reads as I"
    assert matcher.fold_text("ＩſІ") == "IsI", "the fold of before stays"
    assert matcher.fold_text("│।") == "│।", "a box border and a danda stay what they are"


def test_the_generator_keeps_letters_and_leaves_symbols_digits_marks_and_folded_letters_alone():
    """Finding 17: the rules of awb/confusables.py on lines in the form of the data file."""
    data = "\n".join([
        "A4DC ;\t005A ;\tMA\t# ( ꓜ → Z ) LISU LETTER DZA → LATIN CAPITAL LETTER Z\t# ",
        "A4F2 ;\t006C ;\tMA\t# ( ꓲ → l ) LISU LETTER I → LATIN SMALL LETTER L\t# ",
        "A781 ;\t006C ;\tMA\t# ( ꞁ → l ) LATIN SMALL LETTER TURNED L → LATIN SMALL LETTER L\t# ",
        "2502 ;\t006C ;\tMA\t# ( │ → l ) BOX DRAWINGS LIGHT VERTICAL → LATIN SMALL LETTER L\t# ",
        "0661 ;\t006C ;\tMA\t# ( ١ → l ) ARABIC-INDIC DIGIT ONE → LATIN SMALL LETTER L\t# ",
        "0363 ;\t0061 ;\tMA\t# ( ͣ → a ) COMBINING LATIN SMALL LETTER A → LATIN SMALL LETTER A\t# ",
        "017F ;\t0066 ;\tMA\t# ( ſ → f ) LATIN SMALL LETTER LONG S → LATIN SMALL LETTER F\t# ",
        "0031 ;\t006C ;\tMA\t# ( 1 → l ) DIGIT ONE → LATIN SMALL LETTER L\t# ",
        "0433 ;\t0072 ;\tMA\t# ( г → r ) CYRILLIC SMALL LETTER GHE → LATIN SMALL LETTER R\t# ",
        "AB48 ;\t0072 0072 ;\tMA\t# ( ꭈ → rr ) LATIN SMALL LETTER DOUBLE R → rr\t# ",
        "1D1A ;\t1D0B ;\tMA\t# ( ᴚ → ᴋ ) LATIN LETTER SMALL CAPITAL TURNED R → K\t# ",
    ])
    table = confusables.latin_table(data)
    assert table == {"ꓜ": "Z", "ꓲ": "I", "ꞁ": "l", "г": "r", "ᴚ": "k"}
    text = confusables.render(table, "confusables.txt 0.0 of 2026-01-01")
    space: dict = {}
    exec(compile(text, "<generated>", "exec"), space)   # noqa: S102  (the generated module, read back)
    assert space["LATIN"] == table and text.isascii()


# --------------------------------------------------------------------------- P3: structured data on /kb


def test_an_entry_written_past_kb_add_with_a_mail_address_is_withheld_where_shown(home, register_path):
    """b584f6d promises that /kb and the kb_find tool of Ask pass every entry through kb.screen before it is shown.
    kb.screen counts name hits only, on the premise that structured data passed the checks of kb add. Attack: the
    work user owns tcp-kb, so a hostile project session writes an entry file straight into entries/ (no kb add,
    no commit hook runs for an uncommitted file) with a person's mail address; kb.screen hands it to the reader
    page and to the model."""
    e = kb.add("The invented cluster service offers three flavors in the test region.", scope="tcp",
               tags=["cce"], grade="live", cls="stable", source="an invented live check", where=home,
               register_path=register_path)
    path = e.path
    text = path.read_text(encoding="utf-8")
    planted = "xqarv.pomblet@example.org"
    path.write_text(text.rstrip("\n") + " Ask %s for the quota.\n" % planted, encoding="utf-8")
    entry = kb.get(e.id, home)
    assert any(h.get("cls") != "name" for h in check.check_text(entry.statement, register_path)), \
        "setup: the planted address is no structured hit"
    shown, withheld = kb.screen([entry], register_path)
    assert not shown and withheld == 1, "a planted structured hit passed kb.screen"


@pytest.mark.parametrize("planted", [fx.CUSTOMER_CODE, "tcp-q7m4", "/home/" + "xqarv/notes.txt"])
def test_an_entry_written_past_kb_add_with_a_code_or_a_home_path_is_withheld(home, register_path, planted):
    """P3: what kb add refuses (a register or project code, a home path) is withheld when it was
    written into the entry file past kb add; the clean entry next to it is shown."""
    clean = kb.add("The invented block storage offers two disk types in the test region.", scope="tcp",
                   tags=["evs"], grade="live", cls="stable", source="an invented live check", where=home,
                   register_path=register_path)
    e = kb.add("The invented cluster service offers three flavors in the test region.", scope="tcp",
               tags=["cce"], grade="live", cls="stable", source="an invented live check", where=home,
               register_path=register_path)
    text = e.path.read_text(encoding="utf-8")
    e.path.write_text(text.rstrip("\n") + " Seen with %s.\n" % planted, encoding="utf-8")
    shown, withheld = kb.screen([kb.get(clean.id, home), kb.get(e.id, home)], register_path)
    assert [x.id for x in shown] == [clean.id] and withheld == 1


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
