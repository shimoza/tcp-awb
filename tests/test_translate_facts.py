"""the translation check keeps facts, not English spellings (task TR): a number with its unit, a date, a number
format and an identifier inside a hyphenated word are one fact across languages, and a changed value is still
refused."""
from __future__ import annotations

import pytest

from awb import writing


def differ(before: str, after: str) -> list[str]:
    return [k.kind for k in writing.keep(before, after, across=True) if not k.same]


@pytest.mark.parametrize("before,after", [
    ("the term is 12-month.", "die Laufzeit ist 12 Monate."),
    ("the term is 12 months.", "die Laufzeit ist 12 Monaten."),
    ("the term is 12 months.", "срок составляет 12 месяцев."),
    ("notice of 2 weeks, 3 days, 5 hours and 1 year.", "frist von 2 Wochen, 3 Tagen, 5 Stunden und 1 Jahr."),
    ("notice of 2 weeks, 3 days, 5 hours and 1 year.", "срок 2 недели, 3 дня, 5 часов и 1 год."),
    ("it has 4 vCPU, 2 TB, 500 GB and 100 Mbit/s.", "es hat 4 vCPUs, 2 TB, 500 GB und 100 Mbit/s."),
    ("the column is EUR/month.", "die Spalte ist EUR/Monat."),
    ("the column is EUR/month.", "колонка EUR/мес."),
    ("the column is EUR/hour.", "die Spalte ist EUR/Stunde."),
    ("the date is 2026-10-21.", "das Datum ist 21.10.2026."),
    ("the date is 2026-10-21.", "das Datum ist 21.10.26."),
    ("the host is g7v.", "der g7v-Host."),
    ("host g7v", "g7v-Host"),
    ("the CAD session hosts.", "die CAD-Session-Hosts."),
    ("the NVIDIA license.", "die NVIDIA-Lizenz."),
    ("total 1,340.15 EUR.", "summe 1.340,15 EUR."),
    ("total 1,340.15 EUR.", "summe 1 340,15 EUR."),
    ("total 1.340,15 EUR.", "total 1,340.15 EUR."),
    ("it costs 2.5 EUR and 1,000 GB.", "es kostet 2,5 EUR und 1.000 GB."),
    ("region eu-de-1.", "region eu-de-1."),
])
def test_the_same_fact_in_the_form_of_the_other_language_passes(before, after):
    assert differ(before, after) == []


def test_the_four_real_cases_of_the_task_pass():
    english = "term 12-month on host g7v from 2026-10-21, column EUR/month."
    german = "laufzeit 12 Monate auf g7v-Host ab 21.10.2026, Spalte EUR/Monat."
    assert differ(english, german) == []


@pytest.mark.parametrize("before,after,kind", [
    ("a term of 12 months.", "eine Laufzeit von 24 Monaten.", "number"),
    ("a term of 12 months.", "eine Laufzeit von 12 Wochen.", "number"),
    ("a term of 12 months.", "eine Laufzeit von 12.", "number"),
    ("a 12-month term.", "eine Laufzeit von 24 Monate.", "number"),
    ("the column is EUR/month.", "die Spalte ist EUR/Tag.", "identifier"),
    ("the column is EUR/month.", "die Spalte ist EUR.", "identifier"),
    ("the date is 2026-10-21.", "das Datum ist 22.10.2026.", "number"),
    ("the date is 2026-10-21.", "das Datum ist 21.11.2026.", "number"),
    ("the date is 2026-10-21.", "das Datum ist 21.10.2027.", "number"),
    ("the date is 2026-10-11.", "das Datum ist 10.11.2026.", "number"),
    ("the date is 2026-10-21.", "das Datum ist 2026.", "number"),
    ("the host is g7v.", "der g7-Host.", "identifier"),
    ("the host is g7v.", "der Host.", "identifier"),
    ("the CAD session hosts.", "die Session-Hosts.", "identifier"),
    ("the NVIDIA license.", "die Lizenz.", "identifier"),
    ("the NVIDIA license.", "die AMD-Lizenz.", "identifier"),
    ("total 1,340.15 EUR.", "summe 1.340,16 EUR.", "number"),
    ("total 1,340.15 EUR.", "summe 1.340,51 EUR.", "number"),
    ("total 1,340.15 EUR.", "summe 13.401,5 EUR.", "number"),
    ("it has 100 Mbit/s.", "es hat 10 Mbit/s.", "number"),
    ("it has 4 vCPU.", "es hat 8 vCPU.", "number"),
    ("it has 2 TB.", "es hat 2 GB.", "number"),
    ("region eu-de-1.", "region eu-de-2.", "identifier"),
])
def test_a_changed_value_is_still_refused(before, after, kind):
    assert kind in differ(before, after)


def test_numbers_are_read_by_one_rule():
    read = writing._number_of
    assert read("1,340.15") == read("1.340,15") == read("1 340,15") == read("1 340,15") == "1340.15"
    assert read("2,5") == read("2.5") == "2.5"
    assert read("1.000") == read("1,000") == "1000"
    assert read("0.500") == "0.500"
    assert read("1.2.3") == "1.2.3"


def test_within_one_language_nothing_is_read_across():
    assert [k.kind for k in writing.keep("a", "b")] == list(writing.KEEP_KINDS)
    assert not all(k.same for k in writing.keep("the term is 12-month.", "the term is 12 months."))
