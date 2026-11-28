"""DMZ 1 follow-up (B-090, ask 2026-11-26-tcp-6edq-2): a German text of benchmark results is not over-wiped.

The entry keeps registered names and real secrets only. A learned form never matches a word of the dictionary lists
or a vendor name, a run of capitalised German nouns is no name without a first name in it, a match needs word
boundaries on both sides, an uppercase word is a BIC only with a country code at its positions 5 and 6 and no
dictionary word, a docx keeps its paragraphs as text, and the import summary and the report use the same class words.
Every name is an invented fixture name. A "_" in a text below stands for a blank between two capitalised words: the
self-clean scan reads a run of capitalised words in a test file as a name.
"""
from __future__ import annotations

import dataclasses
import io
import re
from pathlib import Path

import docx
import pytest

from awb import bucket, config, images, importcmd, patterns, projects, report, start, wipe
from awb.extract import office
from tests import fixtures as fx
from tests.test_images import png

GOAL = "compare the benchmark results of two graphics cards"


def _t(text: str) -> str:
    return text.replace("_", " ")


GERMAN = _t("""\
# Benchmark der Grafikkarte

MESSMETHODE

Der_Benchmark lief auf der Grafikkarte mit Terraform und Huawei_Cloud. Lauf 1 und Durchlauf 2 brachten ein
gutes Ergebnis, der Durchgang 3 auch.

Lauf_Durchlauf_Durchgang

RFO Benchmark

Fazit_Bewertung

Ansicht_Arbeiten_Mehrere_Fenster

Die_Bewertung der Ansicht ist gut. Mehrere_Läufe zeigen das Fazit: Arbeiten mit der Grafikkarte geht schnell.
Der_Benchmark und die Grafikkarte stehen im Fazit.
""")


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


def _reader(lines):
    it = iter(lines)
    return lambda: next(it, "")


@pytest.fixture
def project(home, capsys):
    assert start.start(home, "project", GOAL, "new", read=_reader(["\n"]), tty=True) == 0
    (pr,) = projects.load(home)
    capsys.readouterr()
    return pr


def _copy(home, cust) -> str:
    files = [x for x in (home.outbox / cust).rglob("F-*") if x.is_file()]
    assert files, sorted(x.name for x in (home.outbox / cust).rglob("*"))
    return "\n".join(x.read_text(encoding="utf-8", errors="replace") for x in files)


def _import(home, pr, tmp_path, files: dict[str, str | bytes], **kwargs) -> str:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    paths = []
    for name, data in files.items():
        (src / name).write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        paths.append(src / name)
    assert importcmd.run(home, pr.code, paths, tty=True, **kwargs) == 0
    return _copy(home, pr.customer)


def _body(copy: str) -> str:
    """The copy without the header block of its file."""
    return re.sub(r"(?m)^# F-[A-Z0-9]{4}\n\n(?:- .*\n)+\n", "", copy)


def _no_token(copy: str) -> None:
    for token in ("[person", "[place", "[company", "[name", "[secret", "BIC-", "URL-", "IP-"):
        assert token not in copy, token


# --------------------------------------------------------------------------- the German text


def test_a_german_benchmark_text_comes_out_unchanged(home, project, tmp_path, capsys):
    copy = _import(home, project, tmp_path, {"benchmark.md": GERMAN})
    assert "wiped: nothing" in capsys.readouterr().out
    assert _body(copy).strip() == GERMAN.strip()


def test_a_registered_name_in_the_german_text_still_becomes_its_code(home, project, tmp_path):
    text = GERMAN + _t("\nDer_Test lief bei %s mit %s.\n") % (fx.CUSTOMER_FORMS[0], fx.PERSON_FORMS[0])
    copy = _import(home, project, tmp_path, {"benchmark.md": text})
    for form in fx.CUSTOMER_FORMS[:1] + fx.PERSON_FORMS:
        assert form not in copy
    assert fx.CUSTOMER_CODE in copy and fx.PERSON_CODE in copy
    assert _t("Der_Benchmark lief auf der Grafikkarte") in copy and "MESSMETHODE" in copy
    assert "[name" not in copy and "[person" not in copy


def test_a_bic_of_a_fixture_bank_is_still_a_finding(home, project, tmp_path):
    copy = _import(home, project, tmp_path, {"benchmark.md": GERMAN + "\nBIC: %s\n" % fx.BANK_BIC})
    assert fx.BANK_BIC not in copy and "BIC-" in copy
    assert "MESSMETHODE" in copy


# --------------------------------------------------------------------------- the rules


def test_a_run_of_german_nouns_needs_a_first_name_in_a_german_text():
    german = _t("Die_Messung zeigt das Ergebnis der Grafikkarte im Vergleich mit dem alten Modell und die Werte sind in "
                "der Tabelle für den Test klar zu lesen und gut zu vergleichen. %s\nDer_Test ist gut und die Werte sind klar.\n")
    assert wipe.wipe_values(german % _t("Lauf_Durchlauf_Durchgang"), []) == []
    assert wipe.wipe_values(german % _t("Qwertzmann_Brinkhove_Zrbl"), []) == []
    assert wipe.wipe_values(german % ("%s Vrelkam" % fx.LISTED_FIRST), []) == ["%s Vrelkam" % fx.LISTED_FIRST]
    assert wipe.wipe_values(german % _t("Stadt_%s") % fx.CONTROL_UNREGISTERED, []) == [fx.CONTROL_UNREGISTERED]
    english = _t("The_test shows the result. %s\nThe values are clear and the run is good.\n")
    assert wipe.wipe_values(english % _t("Qwertzmann_Brinkhove"), []) == [_t("Qwertzmann_Brinkhove")]


@pytest.mark.parametrize("value", ["Fazit_Bewertung", "Benchmark", "Lauf", "Durchlauf_Durchgang", "Ansicht", "Arbeiten",
                                   "Mehrere", "Grafikkarte", "Huawei", "Hardware", "Karte"])
def test_a_learned_form_never_matches_a_dictionary_word_or_a_vendor_name(value):
    value = _t(value)
    state = wipe.WipeState()
    state.learned.learn("person", value, ("person", "1"))
    state.learned.learn("unknown", value, ("unknown", "2"), unknown_only=True)
    state.learned.learn("company", value, ("company", "3"))
    text = _t("Der_Benchmark im Lauf, der Durchlauf, das Fazit, die Bewertung, die Ansicht, Arbeiten, Mehrere, "
              "Grafikkarte, Huawei und Hardware auf der Karte.")
    assert state.learned.find(text, lambda a, b: False) == []


# test_a_learned_surname_still_matches_as_a_whole_word_only is replaced by
# test_g8_a_learned_surname_matches_at_a_hyphen_a_digit_and_a_case_change_but_never_inside_a_plain_word of
# tests/test_review_1128_wipe.py (finding G8 of the review of 2026-11-28)


def _classes(text: str) -> list[str]:
    return [s.cls for s in patterns.find_structured(text)]


@pytest.mark.parametrize("word,found", [
    (fx.BANK_BIC + "XXX", True), (fx.BANK_BIC[:6] + "12", True), (fx.BANK_BIC, False), ("MESSMETHODE", False),
    ("HINTERGRUND", False), ("SCHLUSSWORT", False), ("ZIELSETZUNG", False), (fx.BANK_BIC[:4] + "ER" + "XXXXX", True),
    ("ZUSAMMENFASSUNG", False), ("ABSCHLUSS", False), ("QWXZXXFFXXX", False), ("QWXZXXF1", False),
])
def test_a_bic_needs_a_country_code_and_no_dictionary_word(word, found):
    assert ("bic" in _classes(_t("Das_Wort %s steht hier.") % word)) == found


def test_a_bic_with_a_label_needs_a_country_code_as_well():
    assert _classes("BIC: %s" % fx.BANK_BIC) == ["bic"]
    assert _classes("BIC: MESSMETHODE") == []


# --------------------------------------------------------------------------- the docx


def _docx(logo_heading: bool = False) -> bytes:
    d = docx.Document()
    if logo_heading:
        d.add_heading("", 1).add_run().add_picture(io.BytesIO(png((200, 10, 10))))
    d.add_heading("Messergebnisse", 1)
    d.add_paragraph(_t("Der_Benchmark lief auf der Grafikkarte."))
    d.add_paragraph(_t("Erster_Punkt der Liste"), style=_t("List_Bullet"))
    d.add_paragraph(_t("Zweiter_Punkt der Liste"), style=_t("List_Number"))
    d.add_picture(io.BytesIO(png((10, 20, 30))))
    d.add_paragraph(_t("Das_Fazit steht am Ende."))
    d.add_heading("Anhang", 2)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_a_docx_keeps_its_paragraphs_as_text_and_leaves_no_empty_heading(home, project, tmp_path):
    copy = _body(_import(home, project, tmp_path, {"benchmark.docx": _docx(logo_heading=True)}))
    lines = copy.splitlines()
    assert "# Messergebnisse" in lines and "## Anhang" in lines
    assert _t("Der_Benchmark lief auf der Grafikkarte.") in lines
    assert _t("Das_Fazit steht am Ende.") in lines
    for line in lines:
        assert not re.fullmatch(r"\s*#+\s*", line), repr(line)
    assert sum(1 for line in lines if line.startswith("#")) == 2
    _no_token(copy)


def test_a_docx_with_text_has_no_note_about_a_missing_text_layer(home, project, tmp_path):
    _import(home, project, tmp_path, {"benchmark.docx": _docx()})
    text = (home.outbox / project.customer / "intake-report.md").read_text(encoding="utf-8")
    assert "no text layer" not in text and "review the original" not in text
    assert "picture(s) held" in text and "then into input/F-" in text and "-img/ of the project" in text


def test_only_a_heading_style_makes_a_heading():
    ctx = office._Ctx(styles={"Kapitel": ("Kapitel", 0, ""), "Kapitel2": ("Kapitel 2", None, "Kapitel"),
                              "Heading3": ("Listenpunkt", None, ""), "ListBullet": (_t("List_Bullet"), None, "")})
    assert office._heading_level("Kapitel", ctx) == 1 and office._heading_level("Kapitel2", ctx) == 1
    assert office._heading_level("Heading3", ctx) is None and office._heading_level("ListBullet", ctx) is None
    assert office._heading_level("Heading2", ctx) == 2 and office._heading_level("Title", ctx) == 1
    assert office._heading_level("Normal", ctx) is None


# --------------------------------------------------------------------------- the report and the pictures


def test_the_summary_and_the_report_use_the_same_class_words(home, project, tmp_path, capsys):
    text = "Herr %s und %s. %s sprach mit %s in %s.\n" % (fx.PLANTED_PERSON, fx.CUSTOMER_FORMS[0], fx.SECOND_PERSON,
                                                         fx.PLANTED_CANDIDATE, fx.PLANTED_STREET)
    _import(home, project, tmp_path, {"a.md": text})
    summary = re.search(r"wiped: (.*)", capsys.readouterr().out).group(1)
    assert summary == "person 1, company 1, place 1, name 1"
    row = re.search(r"\| ok \| (.*?) \|", (home.outbox / project.customer / "intake-report.md").read_text())
    assert row.group(1) == "person 1, company 1, place 1, name 1, registered 1"
    assert "unknown" not in row.group(1)
    assert report.CLASS_WORDS["unknown"] == "name"


def _held_docx(home, project, tmp_path) -> str:
    _import(home, project, tmp_path, {"benchmark.docx": _docx()})
    (fid,) = {row["file"] for row in images.held(home, project.customer)}
    return fid


def test_a_released_picture_lands_in_input_of_the_project(home, project, tmp_path, capsys):
    fid = _held_docx(home, project, tmp_path)
    out = images.release(home, project.customer, fid)
    folder = Path(project.path) / "input" / ("%s-img" % fid)
    assert out and all(f.parent == folder for f in out) and all(f.is_file() for f in out)
    assert sorted(f.name for f in out) == sorted(f.name for f in folder.iterdir())
    assert not (home.outbox / project.customer / "images").exists()
    capsys.readouterr()
    assert images.main(["release", project.customer, fid, "--all"]) == 0
    assert "of %s released to input/%s-img/ of the project %s" % (fid, fid, project.code) in capsys.readouterr().out


def test_a_customer_with_two_projects_names_the_project(home, project, tmp_path, monkeypatch):
    fid = _held_docx(home, project, tmp_path)
    second = tmp_path / "second"
    second.mkdir()
    other = dataclasses.replace(project, code="tcp-zz9y", path=str(second))
    monkeypatch.setattr(projects, "load", lambda p: [project, other])
    with pytest.raises(images.ImageError, match="name one with --project"):
        images.release(home, project.customer, fid)
    out = images.release(home, project.customer, fid, project=other.code)
    assert out and all(f.parent == second / "input" / ("%s-img" % fid) for f in out)
    with pytest.raises(images.ImageError, match="is no project of"):
        images.release(home, project.customer, fid, project="tcp-nope")
