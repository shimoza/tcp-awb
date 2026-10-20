"""NQ: the intake asks the owner about obvious non-names (plain English words, hardware and hypervisor terms, shell
fragments, section labels) and still finds a planted person next to them. Words are written as single words or joined
from tuples, so that tests/test_self_clean.py reads them as words. Nothing here prints a planted value."""
from __future__ import annotations

import pytest

from awb import intake, words
from tests import fixtures as fx

CHIP = " ".join(("Sapphire", "Rapids"))
GUARD = " ".join(("Credential", "Guard"))
SHELL = "grep -Ec 'vmx|svm' /proc/cpuinfo"
FIRSTS = ("Mark", "Bill")   # first names of rules/first-names.txt that are also English words


def _values(text: str) -> list[str]:
    return [text[s.start:s.end] for s in intake._collect(text, [])]


HEADED = ("Outcome", "Worked", "KVM", "APPEARED", "Scorecard", "Sapphire", "Sapphire-Rapids", CHIP, GUARD,
          "grep -Ec 'vmx", "D. Built", "A. Scope")


def test_the_nine_words_of_the_report_give_no_candidate():
    # each one as a heading with a line of prose below (the shape that asked the owner), then in running text
    text = "".join("## %s\n\nThe run is described below.\n\n" % h for h in HEADED)
    text += "\n".join((
        "The CPU is %s based." % "Sapphire-Rapids",
        "Hypervisor: KVM and Hyper-V.",
        "Run `%s` first." % SHELL,
        "$ " + SHELL,
        SHELL, "",
    ))
    assert _values(text) == []


def test_a_planted_person_next_to_them_is_still_found():
    text = "\n".join(("Outcome", "Worked on KVM", "Run `%s`" % SHELL, "Scorecard of %s" % fx.PLANTED_PERSON,
                      "APPEARED %s" % CHIP, "%s from %s" % (fx.SECOND_PERSON, fx.PLANTED_CANDIDATE)))
    found = _values(text)
    assert fx.PLANTED_PERSON in found and fx.SECOND_PERSON in found and fx.PLANTED_CANDIDATE in found
    assert not {"Outcome", "Worked", "KVM", "Scorecard", "APPEARED"} & set(found)


def test_a_first_name_that_is_also_an_english_word_stays_a_candidate():
    for first in FIRSTS:
        assert first in _values("Notes\n\n%s joined the call.\n" % first)
    assert fx.FIRST_LAST in _values("Present: %s\n" % fx.FIRST_LAST)


def test_two_capitalised_words_in_a_row_stay_a_candidate_unless_the_phrase_is_known():
    assert fx.PLANTED_PERSON in _values("Outcome of %s today.\n" % fx.PLANTED_PERSON)
    # a run of two plain words is not asked about when the phrase is known; an unknown run still is
    assert _values("The %s are fine.\n" % CHIP) == []
    assert _values(" ".join(("Outcome", "Worked")) + "\n") != []


def test_a_section_label_is_no_initial_but_an_initial_before_an_invented_surname_is():
    assert _values("D. Built\nA. Scope\n") == []
    assert any(fx.PLANTED_PERSON.split()[1] in v for v in _values("J. %s\n" % fx.PLANTED_PERSON.split()[1]))


def test_code_gives_no_candidates_and_prose_after_it_still_does():
    text = "`%s`\n$ ls -la /tmp\n```\nOutcome Worked\n```\nThen %s spoke.\n" % (SHELL, fx.PLANTED_PERSON)
    assert _values(text) == [fx.PLANTED_PERSON]


def test_the_new_lists_load_and_a_first_name_is_dropped_from_them():
    common = words.load_plain(words.COMMON_FILE)
    assert len(common) > 20000 and {"outcome", "worked", "scorecard", "appeared"} <= common
    assert not {w.casefold() for w in fx.PLANTED_PERSON.split()} & common
    tech = words.load_plain(words.TECH_FILE)
    assert {"kvm", "qemu", "hyper-v", "vt-x", "sapphire rapids", "credential guard", "tpm"} <= tech
    from awb.intake.wipe import lexicon
    assert not lexicon._COMMON_WORDS & lexicon._FIRST_NAMES and not lexicon._TECH_TERMS & lexicon._FIRST_NAMES
    assert words.load_plain(fx_missing := words.COMMON_FILE.with_name("absent.txt")) == set() and not fx_missing.exists()


@pytest.mark.parametrize("name", ("common-words.txt", "tech-terms.txt"))
def test_the_new_rules_files_say_where_they_come_from(name):
    head = (words.COMMON_FILE.parent / name).read_text(encoding="utf-8").split("\n\n", 1)[0]
    assert head.startswith("#") and ("public domain" in head or "written by hand" in head)
