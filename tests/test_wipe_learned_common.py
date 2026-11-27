"""B-117: learned forms never spread a common word over a whole import run.

The import of old material wiped "Max." in table headers, sentence openers (This, It's, That's), a platform product
name and German nouns (Grundpreis, Bedingungen) in every file of the run, because one false seed taught a learned form.
These tests run the rules over invented texts of the same shapes; the names come from tests/fixtures.py.
"""
from __future__ import annotations

from awb.intake.wipe import runstate as rs
from tests import fixtures as fx


def _run(*texts: str) -> tuple[rs.WipeState, list[list[str]]]:
    """Learn every text like one import run, then wipe each: the state and the wiped values of each text."""
    state = rs.WipeState()
    for t in texts:
        rs.learn(state, t, [], [])
    out = []
    for t in texts:
        out.append([t[w.start:w.end] for w in rs.collect(t, [], state)])
    return state, out


FIRST, LAST = fx.WORD_FIRST_PERSON.split()


def test_max_of_a_person_never_wipes_max_dot_or_max_identifiers():
    person = "Ansprechpartner: %s\n\nDie Firewall hat %s gebaut. Danach hat %s die Regeln geprüft.\n" % (
        fx.WORD_FIRST_PERSON, fx.WORD_FIRST_PERSON, LAST)
    table = ("| Modell | Max. Input TPM | Max. Output TPM | Max. RPM |\n| --- | --- | --- | --- |\n| a | 1 | 2 | 3 |\n"
             "Es wird darauf hingewiesen, dass es sich bei den Werten (Max. Input TPM, Max. Output TPM) um Limits "
             "handelt.\n"
             '{"min_cu": 16, "max_cu": 16, "Max_concurrency": 4}\n'
             "This is quite common.\nThis note describes the setup. This one is about being ready.\n")
    state, (wiped_person, wiped_table) = _run(person, table)
    assert LAST.casefold() in state.learned.words
    assert any(LAST in v for v in wiped_person)
    assert wiped_table == []


def test_first_name_next_to_a_wiped_surname_still_goes_at_a_line_start():
    one = "Kontakt: %s\n" % fx.WORD_FIRST_PERSON
    two = "%s %s\n%s. Output TPM\n" % (FIRST, LAST, FIRST)
    _, (_, wiped) = _run(one, two)
    joined = " ".join(wiped)
    assert FIRST in joined and LAST in joined   # the person
    assert joined.count(FIRST) == 1             # Max. stays


def test_transcript_sentence_openers_are_never_learned():
    lines = ["It's downloaded.", "That's why you have 25 of them.", "They're all trying to get away from it.",
             "You're familiar with that.", "This is quite common.", "There's more.", "So that's it."]
    stamped = "".join("00:%02d:10 %s\n" % (i, line) for i, line in enumerate(lines))
    legend = "Speaker 1: That's it.\n\nSpeaker 2: There's more, it's a matter of 25.\n"
    speaker = "%s: That's the challenge. That's why it fails.\n" % fx.FIRST_LAST
    prose = "\n".join(lines) + "\nBut when you're talking to a corporation, they're careful. That's it.\n"
    state, wiped = _run(stamped, legend, speaker, prose)
    learned = set(state.learned.words)
    assert not learned & {"it's", "that's", "they're", "you're", "there's", "this", "that", "familiar"}
    assert wiped[0] == [] and wiped[1] == [] and wiped[3] == []
    assert wiped[2] == [fx.FIRST_LAST]


def test_frau_klein_stays_wiped_and_the_adjective_stays():
    first = "Frau %s hat das Angebot geprüft.\n" % fx.WORD_SURNAME
    second = ("Leider ist das Netz zu %s für den Cluster. Wir warten auf %s und die Freigabe.\n"
              % (fx.WORD_SURNAME.casefold(), fx.WORD_SURNAME))
    _, (one, two) = _run(first, second)
    assert any(fx.WORD_SURNAME in v for v in one)
    assert two == [fx.WORD_SURNAME]


def test_frau_klein_at_a_line_start_with_a_title_still_goes():
    first = "Frau %s hat das Angebot geprüft.\n" % fx.WORD_SURNAME
    second = "Hallo zusammen,\nFrau %s schickt das Angebot morgen.\n" % fx.WORD_SURNAME
    _, (_, two) = _run(first, second)
    assert any(fx.WORD_SURNAME in v for v in two)


def test_german_noun_under_a_label_teaches_no_form():
    contract = ("Ansprechpartner: Grundpreis\n"
                "Der monatliche Grundpreis für Modelle im Tarif S1000 beträgt 100 Euro. Mit dem "
                "Grundpreis berechnet der Anbieter auch Nutzung.\n")
    other = "Preise, Tarife, Grundpreis\n25% des vereinbarten Grundpreis\n"
    state, (_, wiped) = _run(contract, other)
    assert "grundpreis" not in state.learned.words
    assert wiped == []


def test_platform_product_names_are_no_person():
    text = ("AI Foundation Services (AIFS), technischer Alias LLM Hub, ist ein Dienst.\n"
            "Owner: AIFS\n\nThe AIFS team answers. # AIFS / LLM Hub\n"
            "Alibaba hosts QwenVL-Max and Jina AI hosts jina-embeddings-v2-base-de\n")
    later = "Ask the AIFS team about QwenVL-Max and Qwen 2.5 VL 72B.\n"
    state, (_, wiped) = _run(text, later)
    assert not set(state.learned.words) & {"aifs", "qwen", "alibaba", "jina", "max"}
    assert wiped == []


def test_short_surname_with_an_s_is_the_english_word():
    person = "Kontakt: %s %s\n" % (fx.LISTED_FIRST, fx.SHORT_SURNAME)
    later = "Plus one more node. We asked %s about it.\n" % fx.SHORT_SURNAME
    state, (_, wiped) = _run(person, later)
    assert fx.SHORT_SURNAME.casefold() in state.learned.words
    assert wiped == [fx.SHORT_SURNAME]


def test_unknown_surname_still_goes_at_a_sentence_start():
    """The guard holds for known words only: an unknown surname is wiped wherever it stands."""
    person = "Kontakt: %s\n" % fx.SECOND_PERSON
    later = "%s hat die Firewall gebaut.\n| %s | Admin |\n" % (fx.SECOND_LAST, fx.SECOND_LAST)
    _, (_, wiped) = _run(person, later)
    assert wiped == [fx.SECOND_LAST, fx.SECOND_LAST]
