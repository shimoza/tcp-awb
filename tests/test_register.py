"""Tests for awb.register. Only invented names from tests/fixtures.py appear here."""
from __future__ import annotations

import io
import os
import stat
import sys

import pytest

from awb import cli, codes, config, register, vault
from tests import fixtures
from tests.test_vault import PASS, encrypt_vault, fast_gpg, serving, sockets  # noqa: F401 (fixtures)

HEADER = "\t".join(register.HEADER)
CUST = fixtures.CUSTOMER_CODE
SHORT = fixtures.CUSTOMER_FORMS[1]
FULL = fixtures.CUSTOMER_FORMS[0]


def _line(code, kind, form, added=fixtures.TODAY, status="active"):
    return "\t".join((code, kind, form, added, status))


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _assert_clean_message(msg: str, *secrets: str) -> None:
    for s in secrets:
        assert s not in msg
    for f in fixtures.ALL_REGISTERED:
        assert f not in msg


def _expect_error(tmp_path, lines, line_no, *secrets):
    path = tmp_path / "register.tsv"
    _write(path, lines)
    with pytest.raises(register.RegisterError) as info:
        register.load(path)
    msg = str(info.value)
    assert ("line %d" % line_no) in msg
    _assert_clean_message(msg, *secrets)
    return msg


# --- load: the fixture register and the strict format ---


def test_fixture_register_loads(register_path):
    entries = register.load(register_path)
    assert len(entries) == len(fixtures.register_lines()) - 1
    assert sum(1 for e in entries if e.status == "retired") == 1
    assert all(isinstance(e, register.Entry) for e in entries)
    by_code = {}
    for e in entries:
        by_code.setdefault(e.code, []).append(e.form)
    assert set(by_code[CUST]) == set(fixtures.CUSTOMER_FORMS)
    assert by_code[fixtures.PERSON_CODE] == fixtures.PERSON_FORMS


def test_missing_file_is_empty(tmp_path):
    assert register.load(tmp_path / "none.tsv") == []


def test_unreadable_file_raises(tmp_path):
    with pytest.raises(register.RegisterError):
        register.load(tmp_path)  # a directory
    bad = tmp_path / "bad.tsv"
    bad.write_bytes(b"\xff\xfe\x00 not utf-8")
    with pytest.raises(register.RegisterError):
        register.load(bad)


def test_bracket_note_in_form(tmp_path):
    form = SHORT + " (alter Name)"
    msg = _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", FULL), _line(CUST, "CUST", form)], 3, form, "alter Name")
    assert "line 2" not in msg


def test_square_bracket_in_form(tmp_path):
    form = SHORT + " [x]"
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", form)], 2, form)


def test_pipe_in_form(tmp_path):
    form = SHORT + " | " + SHORT.lower()
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", form)], 2, form)


def test_hash_in_form(tmp_path):
    form = SHORT + " #1"
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", form)], 2, form)


def test_comment_line(tmp_path):
    comment = "# " + SHORT + " was added by hand"
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", FULL), comment], 3, comment)


def test_wrong_header(tmp_path):
    bad_header = "code\tkind\tform\tadded"
    _expect_error(tmp_path, [bad_header, _line(CUST, "CUST", FULL)], 1)
    _expect_error(tmp_path, ["Code\tKind\tForm\tAdded\tStatus", _line(CUST, "CUST", FULL)], 1)
    with pytest.raises(register.RegisterError):
        register.load(_written(tmp_path, ""))


def _written(tmp_path, content):
    p = tmp_path / "r.tsv"
    p.write_text(content, encoding="utf-8")
    return p


def test_four_fields(tmp_path):
    four = "\t".join((CUST, "CUST", FULL, fixtures.TODAY))
    msg = _expect_error(tmp_path, [HEADER, four], 2, FULL)
    assert "4" in msg


def test_six_fields(tmp_path):
    six = _line(CUST, "CUST", FULL) + "\tnote"
    _expect_error(tmp_path, [HEADER, six], 2, FULL)


def test_unknown_kind(tmp_path):
    _expect_error(tmp_path, [HEADER, _line(CUST, "FOO", FULL)], 2, FULL)


def test_kind_must_match_code(tmp_path):
    _expect_error(tmp_path, [HEADER, _line(CUST, "PART", FULL)], 2, FULL)
    _expect_error(tmp_path, [HEADER, _line(fixtures.PERSON_CODE, "CUST", fixtures.PERSON_FORMS[0])], 2)


def test_bad_code(tmp_path):
    _expect_error(tmp_path, [HEADER, _line(CUST.lower(), "CUST", FULL)], 2, FULL)
    _expect_error(tmp_path, [HEADER, _line("CUST-Q7M", "CUST", FULL)], 2, FULL)
    _expect_error(tmp_path, [HEADER, _line("CUST-Q7M4-PERS", "PERS", FULL)], 2, FULL)


def test_duplicate_code_and_form(tmp_path):
    lines = [HEADER, _line(CUST, "CUST", FULL), _line(CUST, "CUST", SHORT), _line(CUST, "CUST", FULL)]
    msg = _expect_error(tmp_path, lines, 4, FULL)
    assert "line 2" in msg


def test_bad_date(tmp_path):
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", FULL, added="22.09.2026")], 2, FULL)
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", FULL, added="2026-02-30")], 2, FULL)
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", FULL, added="20260922")], 2, FULL)


def test_bad_status(tmp_path):
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", FULL, status="gone")], 2, FULL)


def test_blank_line_inside(tmp_path):
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", FULL), "", _line(CUST, "CUST", SHORT)], 3)


def test_unstripped_or_empty_form(tmp_path):
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", " " + FULL)], 2, FULL)
    _expect_error(tmp_path, [HEADER, _line(CUST, "CUST", "")], 2)


def test_crlf_refused(tmp_path):
    p = tmp_path / "r.tsv"
    p.write_bytes((HEADER + "\r\n" + _line(CUST, "CUST", FULL) + "\r\n").encode("utf-8"))
    with pytest.raises(register.RegisterError) as info:
        register.load(p)
    _assert_clean_message(str(info.value))


# --- save, add, retire ---


def test_add_and_retire_round_trip(tmp_path):
    path = tmp_path / "vault" / "register.tsv"
    assert not path.parent.exists()
    e1 = register.add(path, CUST, "CUST", "  " + FULL + "  ", fixtures.TODAY)
    assert e1 == register.Entry(CUST, "CUST", FULL, fixtures.TODAY, "active")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    e2 = register.add(path, CUST, "CUST", SHORT)
    assert e2.added == fixtures.TODAY or len(e2.added) == 10
    assert register.load(path) == [e1, e2]
    assert [n for n in os.listdir(path.parent) if n != path.name] == []

    assert register.retire(path, CUST) == 2
    entries = register.load(path)
    assert [e.status for e in entries] == ["retired", "retired"]
    assert register.retire(path, CUST) == 0
    assert register.retire(path, "CUST-ZZZZ") == 0
    assert register.codes(entries) == {CUST}
    assert register.forms_for_matching(entries) == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(register.RegisterError) as info:
        register.add(path, CUST, "CUST", fixtures.CUSTOMER_FORMS[2])
    _assert_clean_message(str(info.value))


def test_add_refuses_duplicates_and_bad_forms(register_path):
    before = register.load(register_path)
    with pytest.raises(register.RegisterError) as info:
        register.add(register_path, CUST, "CUST", FULL)
    assert "line 2" in str(info.value)
    _assert_clean_message(str(info.value))
    with pytest.raises(register.RegisterError) as info:
        register.add(register_path, CUST, "CUST", SHORT + " (Notiz)")
    _assert_clean_message(str(info.value), "Notiz")
    with pytest.raises(register.RegisterError):
        register.add(register_path, CUST, "PERS", SHORT + " x")
    with pytest.raises(register.RegisterError):
        register.add(register_path, CUST, "CUST", SHORT + " y", added="2026-9-1")
    assert register.load(register_path) == before


def test_save_refuses_bad_entries_and_keeps_old_file(tmp_path):
    path = tmp_path / "r.tsv"
    good = register.Entry(CUST, "CUST", FULL, fixtures.TODAY, "active")
    register.save(path, [good])
    bad = register.Entry(CUST, "CUST", SHORT + " | " + SHORT, fixtures.TODAY, "active")
    with pytest.raises(register.RegisterError) as info:
        register.save(path, [good, bad])
    assert "line 3" in str(info.value)
    _assert_clean_message(str(info.value))
    assert register.load(path) == [good]
    with pytest.raises(register.RegisterError):
        register.save(path, [good, good])


def test_codes_and_next_code(register_path, monkeypatch):
    entries = register.load(register_path)
    all_codes = register.codes(entries)
    assert CUST in all_codes
    assert "PART-RET2" in all_codes  # retired codes stay reserved
    fresh = register.next_code(entries, "CUST")
    assert codes.is_code(fresh) and codes.kind_of(fresh) == "CUST"
    assert fresh not in all_codes
    ids = iter(["Q7M4", "Q7M4", "AB3D"])
    monkeypatch.setattr(codes, "_id", lambda: next(ids))
    assert register.next_code(entries, "CUST") == "CUST-AB3D"
    ids = iter(["RET2", "RET2", "AB3D"])
    assert register.next_code(entries, "PART") == "PART-AB3D"
    with pytest.raises(ValueError):
        register.next_code(entries, "FOO")


def test_next_sub_code(register_path):
    entries = register.load(register_path)
    assert register.next_sub_code(entries, CUST, "PERS") == CUST + "-PERS-2"
    assert register.next_sub_code(entries, CUST, "REF") == CUST + "-REF-3"
    assert register.next_sub_code(entries, CUST, "DOM") == CUST + "-DOM-2"
    assert register.next_sub_code(entries, fixtures.PARTNER_CODE, "PERS") == fixtures.PARTNER_CODE + "-PERS-1"


# --- forms_for_matching ---


def test_forms_for_matching_umlaut_variants(register_path):
    pairs = register.forms_for_matching(register.load(register_path))
    variants = {v for v, c in pairs if c == fixtures.ORG_CODE}
    short = fixtures.ORG_FORMS[1]
    assert short in variants
    assert short.replace("ö", "oe") in variants
    assert short.replace("ö", "o") in variants
    long_form = fixtures.ORG_FORMS[0]
    assert long_form.replace("ö", "oe").replace("ä", "ae") in variants
    assert long_form.replace("ö", "o").replace("ä", "a") in variants


def test_forms_for_matching_genitive_variants(register_path):
    pairs = register.forms_for_matching(register.load(register_path))
    variants = {v for v, c in pairs if c == CUST}
    for suffix in ("s", "'s", "’s"):
        assert SHORT + suffix in variants
        assert FULL + suffix in variants
    # a form that ends in s gets no genitive
    path = register_path
    ends_in_s = fixtures.CUSTOMER_FORMS[3].rsplit(" ", 1)[0]
    assert ends_in_s.endswith("s")
    register.add(path, CUST, "CUST", ends_in_s)
    variants = {v for v, c in register.forms_for_matching(register.load(path)) if c == CUST}
    assert ends_in_s in variants
    assert ends_in_s + "s" not in variants
    assert ends_in_s + "'s" not in variants


def test_forms_for_matching_whitespace_collapsed(tmp_path):
    path = tmp_path / "r.tsv"
    spaced = FULL.replace(" ", "   ")
    register.add(path, CUST, "CUST", spaced, fixtures.TODAY)
    pairs = register.forms_for_matching(register.load(path))
    variants = {v for v, c in pairs}
    assert spaced in variants
    assert FULL in variants
    assert FULL + "s" in variants


def test_forms_for_matching_longest_first_and_codes(register_path):
    pairs = register.forms_for_matching(register.load(register_path))
    lengths = [len(v) for v, c in pairs]
    assert lengths == sorted(lengths, reverse=True)
    assert len(pairs) == len(set(pairs))
    assert pairs[0][0].startswith(fixtures.LAWFIRM_FORMS[0].split(" ")[0])
    assert (fixtures.PERSON_FORMS[0], fixtures.PERSON_CODE) in pairs
    assert (fixtures.FILE_NUMBER, CUST + "-REF-1") in pairs
    assert (fixtures.TENDER_ID, CUST + "-REF-2") in pairs
    assert (fixtures.CUSTOMER_DOMAIN, CUST + "-DOM-1") in pairs


def test_forms_for_matching_skips_retired(register_path):
    entries = register.load(register_path)
    retired = [e for e in entries if e.status == "retired"]
    assert len(retired) == 1
    pairs = register.forms_for_matching(entries)
    assert all(c != retired[0].code for v, c in pairs)
    assert all(retired[0].form.lower() not in v.lower() for v, c in pairs)


# --- parse, render and the encrypted register (release 2) ---


def test_parse_and_render_follow_the_rules_of_load_and_save(register_path):
    entries = register.load(register_path)
    text = register.render(entries)
    assert text == register_path.read_text(encoding="utf-8")
    assert register.parse(text) == entries == register.parse(text.encode("utf-8"))
    bad = text + _line(CUST, "CUST", SHORT + " (Notiz)") + "\n"
    with pytest.raises(register.RegisterError) as info:
        register.parse(bad)
    assert "line %d" % (len(entries) + 2) in str(info.value)
    _assert_clean_message(str(info.value), "Notiz")
    with pytest.raises(register.RegisterError):
        register.parse(b"\xff\xfe not utf-8")
    with pytest.raises(register.RegisterError):
        register.render(entries + [entries[0]])
    assert register.encrypted_path(register_path).name == "register.tsv.gpg"


def test_load_and_save_go_through_the_vault_daemon_when_encrypted(home, sockets, fast_gpg, monkeypatch):
    p = config.paths()
    before = register.load(p.register)
    with serving(p):
        assert encrypt_vault(monkeypatch) == 0
        assert not p.register.exists() and p.register_encrypted.exists()
        assert register.load(p.register) == before
        e = register.add(p.register, fixtures.PARTNER_CODE, "PART", fixtures.PLANTED_CANDIDATE, fixtures.TODAY)
        assert not p.register.exists(), "a plaintext register was written"
        assert register.load(p.register) == before + [e]
        assert register.retire(p.register, fixtures.PARTNER_CODE) == 1
        after = register.load(p.register)
        assert after[-1].status == "retired" and len(after) == len(before) + 1
        plain = vault.decrypt_bytes(p.register_encrypted.read_bytes(), PASS, p.vault / ".gnupg")
        assert register.parse(plain) == after
        # the register only grows through the daemon
        with pytest.raises(register.RegisterError) as info:
            register.save(p.register, after[1:])
        _assert_clean_message(str(info.value))
        assert register.load(p.register) == after
        assert vault.main(["lock"]) == 0
        with pytest.raises(register.RegisterError) as info:
            register.load(p.register)
        assert "locked" in str(info.value)
        _assert_clean_message(str(info.value), PASS)
    with pytest.raises(register.RegisterError) as info:
        register.load(p.register)
    assert "not available" in str(info.value)
    with pytest.raises(register.RegisterError):
        register.save(p.register, after)
    assert not p.register.exists()


# --- awb register add with several forms, awb register find (owner side only) ---

PART = fixtures.PARTNER_CODE
THREE_FORMS = [fixtures.PLANTED_CANDIDATE, fixtures.SECOND_COMPANY, fixtures.SELFTEST_FORM]


def _owner_terminal(monkeypatch):
    """The owner's own terminal: not a session, stdout a tty (pytest's capture is not one)."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def _partner_forms(path):
    return [e.form for e in register.load(path) if e.code == PART and e.status == "active"]


def test_add_with_three_forms_registers_three(home, capsys):
    assert cli.main(["register", "add", PART, "PART"] + THREE_FORMS) == 0
    assert _partner_forms(home.register) == THREE_FORMS
    out = capsys.readouterr()
    assert out.out == "added 3 form(s) to %s (3 active)\n" % PART
    for f in THREE_FORMS:
        assert f not in out.out + out.err
    # one bad form among several and nothing is added
    assert cli.main(["register", "add", PART, "PART", THREE_FORMS[0] + " x", THREE_FORMS[1] + " (Notiz)"]) == 2
    assert _partner_forms(home.register) == THREE_FORMS
    assert "Notiz" not in capsys.readouterr().err


def test_add_with_a_dash_reads_three_lines_from_standard_input(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(THREE_FORMS) + "\n\n"))
    assert cli.main(["register", "add", PART, "PART", "-"]) == 0
    assert _partner_forms(home.register) == THREE_FORMS
    assert capsys.readouterr().out == "added 3 form(s) to %s (3 active)\n" % PART
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert cli.main(["register", "add", PART, "PART", "-"]) == 2
    assert "no form" in capsys.readouterr().err


def test_find_of_a_registered_form_prints_its_code(home, monkeypatch, capsys):
    _owner_terminal(monkeypatch)
    assert cli.main(["register", "find"] + FULL.split()) == 0
    assert capsys.readouterr().out == "%s CUST\n" % CUST
    # the match is exact after the folding of the matcher: case and umlauts, whitespace collapsed
    assert cli.main(["register", "find", fixtures.ORG_FORMS[1].replace("ö", "o").upper()]) == 0
    assert capsys.readouterr().out == "%s ORG\n" % fixtures.ORG_CODE
    assert cli.main(["register", "find", fixtures.PERSON_FORMS[0].lower()]) == 0
    assert capsys.readouterr().out == "%s PERS\n" % fixtures.PERSON_CODE
    # a part of a form, a genitive and a retired form are no match
    retired = [e.form for e in register.load(home.register) if e.status == "retired"][0]
    for words in (FULL.split()[:2], [SHORT + "s"], retired.split()):
        assert cli.main(["register", "find"] + words) == 0
        assert capsys.readouterr().out == "no code\n"


def test_find_of_the_control_name_prints_no_code(home, monkeypatch, capsys):
    _owner_terminal(monkeypatch)
    assert cli.main(["register", "find", fixtures.CONTROL_UNREGISTERED]) == 0
    out = capsys.readouterr()
    assert out.out == "no code\n" and out.err == ""


def test_find_in_a_pipe_is_refused_with_the_fixed_line(home, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    assert cli.main(["register", "find"] + FULL.split()) == 2      # pytest captures stdout: not a terminal
    out = capsys.readouterr()
    assert out.out == "" and out.err == cli.FIND_REFUSED + "\n"
    _assert_clean_message(out.err)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["register", "find"] + FULL.split()) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err == cli.FIND_REFUSED + "\n"


def test_find_as_the_work_user_is_refused(home, monkeypatch, capsys):
    _owner_terminal(monkeypatch)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert cli.main(["register", "find"] + FULL.split()) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err == cli.FIND_REFUSED + "\n"
