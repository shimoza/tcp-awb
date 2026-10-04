"""awb register review: the candidates of a blocked intake sorted in one pass, the names into the register, the
rest into the keep list, the next intake passes with codes in place of the names and nothing printed but counts."""
from __future__ import annotations

import pytest

from awb import candidates, intake, register
from tests import fixtures as fx

PLACE = "Region Nord Standort"
HEADING = "Technisches Konzept"
TEXT = ("%s\n\nNotes of the training. %s from %s explained the setup at %s. Terraform plans were reviewed.\n"
        % (HEADING, fx.PLANTED_PERSON, fx.PLANTED_CANDIDATE, PLACE))


def _blocked(home, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text(TEXT, encoding="utf-8")
    res = intake.run([f], fx.CUSTOMER_CODE, home)
    assert res.blocked and res.candidates >= 3
    return f


def _marker(marks):
    """An editor stand-in: a mark in front of each line that holds one of the given phrases."""
    def edit(path):
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            phrase = line.strip()
            mark = next((m for text, m in marks.items() if not line.startswith("#") and text in phrase), "")
            lines.append("%s %s" % (mark, phrase) if mark else line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return edit


def test_a_review_registers_the_names_keeps_the_rest_and_the_intake_passes(home, tmp_path):
    f = _blocked(home, tmp_path)
    said = []
    code = candidates.review(home, fx.CUSTOMER_CODE, edit=_marker({fx.PLANTED_PERSON: "p", fx.PLANTED_CANDIDATE: "o",
                                                                   PLACE: "s"}),
                             confirm=lambda question: True, out=said.append)
    assert code == 0
    entries = register.load(home.register)
    kinds = {e.kind: e.code for e in entries if e.form in (fx.PLANTED_PERSON, fx.PLANTED_CANDIDATE, PLACE)}
    assert kinds["PERS"].startswith(fx.CUSTOMER_CODE + "-PERS-") and kinds["SITE"].startswith(fx.CUSTOMER_CODE + "-SITE-")
    assert kinds["ORG"].startswith("ORG-") and kinds["ORG"].count("-") == 1
    kept = intake.load_keep(home)
    assert kept == [HEADING]
    for line in said:
        for name in (fx.PLANTED_PERSON, fx.PLANTED_CANDIDATE, PLACE, HEADING):
            assert name not in line
    assert not list((home.vault / "tmp").glob("review-*"))
    again = intake.run([f], fx.CUSTOMER_CODE, home)
    assert not again.blocked, "the second intake is still blocked"
    out = "".join(o.read_text(encoding="utf-8") for o in again.outputs if o.name != "intake-report.md")
    assert fx.PLANTED_PERSON not in out and fx.PLANTED_CANDIDATE not in out and PLACE not in out and HEADING in out
    assert kinds["PERS"] in out


def test_a_review_without_confirmation_changes_nothing(home, tmp_path):
    _blocked(home, tmp_path)
    before = register.load(home.register)
    said = []
    assert candidates.review(home, fx.CUSTOMER_CODE, edit=lambda path: None, confirm=lambda q: False,
                             out=said.append) == 1
    assert register.load(home.register) == before and intake.load_keep(home) == []
    assert said == ["nothing was changed"]


def test_the_second_review_shows_only_what_is_still_open(home, tmp_path):
    _blocked(home, tmp_path)
    candidates.review(home, fx.CUSTOMER_CODE, edit=_marker({fx.PLANTED_PERSON: "p", fx.PLANTED_CANDIDATE: "-",
                                                            PLACE: "-"}),
                      confirm=lambda q: True, out=lambda s: None)
    open_now = candidates.still_open(home, candidates.parse_candidates(
        candidates.read_report(candidates.latest_report(home, fx.CUSTOMER_CODE))))
    assert any(fx.PLANTED_CANDIDATE in c for c in open_now)
    assert not any(c == fx.PLANTED_PERSON for c in open_now)
    assert HEADING not in open_now and PLACE in open_now


def test_the_marks_of_a_review_file():
    cands = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    d = candidates.parse_review("# comment\np Alpha\n+ Alphas\n   Beta\no Gamma\n-  Delta\n", cands)
    assert d.names == [("p", ["Alpha", "Alphas"]), ("o", ["Gamma"])]
    assert d.keep == ["Beta"] and d.later == 2          # Delta marked later, Epsilon deleted
    assert candidates.parse_review("C Alpha\nc   Beta\n", cands).names == [("c", ["Alpha"]), ("c", ["Beta"])]


@pytest.mark.parametrize("text,reason", [
    ("x Alpha\n", "line 1 starts with a letter"),
    ("+ Alpha\n", "line 1: + needs a marked name"),
    ("   Beta\n   Unknown\n", "line 2 is not one of the candidates"),
])
def test_a_review_file_that_cannot_be_applied_names_the_line_and_never_the_phrase(text, reason):
    with pytest.raises(candidates.ReviewError) as err:
        candidates.parse_review(text, ["Alpha", "Beta"])
    assert reason in str(err.value) and "Alpha" not in str(err.value) and "Unknown" not in str(err.value)


def test_the_candidates_of_a_report_with_an_escaped_pipe():
    report = ("# Private intake report\n\n## Candidates\n\n| part id | candidate |\n| --- | --- |\n"
              "| f1-p1 | Alpha \\| Beta |\n| f1-p2 | alpha \\| beta |\n| f1-p3 | Gamma |\n\n## Dropped items\n\n| x | y |\n")
    assert candidates.parse_candidates(report) == ["Alpha | Beta", "Gamma"]


def test_the_command_runs_only_on_the_owners_own_terminal(home, monkeypatch, capsys):
    from awb import cli

    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["register", "review", fx.CUSTOMER_CODE]) == 2
    assert "assistant session" in capsys.readouterr().err
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    assert cli.main(["register", "review", fx.CUSTOMER_CODE]) == 2
    assert "needs a terminal" in capsys.readouterr().err


def test_a_mistyped_mark_keeps_the_marks_and_opens_the_editor_again(home, tmp_path, monkeypatch):
    """The terminal path with a real editor process: a wrong letter first, the same file again with the reason on
    top and the marks still there, then the fix."""
    import sys
    _blocked(home, tmp_path)
    state = tmp_path / "rounds"
    script = tmp_path / "editor.py"
    script.write_text(
        "import sys, pathlib\n"
        "f, s = pathlib.Path(sys.argv[1]), pathlib.Path(%r)\n"
        "n = int(s.read_text()) if s.exists() else 0\n"
        "s.write_text(str(n + 1))\n"
        "t = f.read_text()\n"
        "if n == 0:\n"
        "    t = t.replace('   ' + %r, 'x ' + %r).replace('   ' + %r, 'o ' + %r)\n"
        "else:\n"
        "    assert t.startswith('# FIX: line') and ('o ' + %r) in t\n"
        "    t = t.replace('x ' + %r, 'p ' + %r)\n"
        "f.write_text(t)\n" % (str(state), fx.PLANTED_PERSON, fx.PLANTED_PERSON, fx.PLANTED_CANDIDATE,
                                 fx.PLANTED_CANDIDATE, fx.PLANTED_CANDIDATE, fx.PLANTED_PERSON, fx.PLANTED_PERSON),
        encoding="utf-8")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "%s %s" % (sys.executable, script))
    said = []
    assert candidates.review(home, fx.CUSTOMER_CODE, confirm=lambda q: True, out=said.append) == 0
    assert state.read_text() == "2" and "editor opens again" in said[0] and fx.PLANTED_PERSON not in said[0]
    forms = {e.form: e.kind for e in register.load(home.register)}
    assert forms[fx.PLANTED_PERSON] == "PERS" and forms[fx.PLANTED_CANDIDATE] == "ORG"
    assert not list((home.vault / "tmp").glob("review-*"))
