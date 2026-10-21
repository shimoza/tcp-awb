"""The translation check keeps facts, not English spellings (task TR): a number with its unit, a date, a number
format and an identifier inside a hyphenated word are one fact across languages, and a changed value is still
refused."""
from __future__ import annotations

import pytest

from awb import writing


def differ(before: str, after: str) -> list[str]:
    return [k.kind for k in writing.keep(before, after, across=True) if not k.same]


@pytest.mark.parametrize("before,after", [
    ("The term is 12-month.", "Die Laufzeit ist 12 Monate."),
    ("The term is 12 months.", "Die Laufzeit ist 12 Monaten."),
    ("The term is 12 months.", "Срок составляет 12 месяцев."),
    ("Notice of 2 weeks, 3 days, 5 hours and 1 year.", "Frist von 2 Wochen, 3 Tagen, 5 Stunden und 1 Jahr."),
    ("Notice of 2 weeks, 3 days, 5 hours and 1 year.", "Срок 2 недели, 3 дня, 5 часов и 1 год."),
    ("It has 4 vCPU, 2 TB, 500 GB and 100 Mbit/s.", "Es hat 4 vCPUs, 2 TB, 500 GB und 100 Mbit/s."),
    ("The column is EUR/month.", "Die Spalte ist EUR/Monat."),
    ("The column is EUR/month.", "Колонка EUR/мес."),
    ("The column is EUR/hour.", "Die Spalte ist EUR/Stunde."),
    ("The date is 2026-10-21.", "Das Datum ist 21.10.2026."),
    ("The date is 2026-10-21.", "Das Datum ist 21.10.26."),
    ("The host is g7v.", "Der g7v-Host."),
    ("Host g7v", "g7v-Host"),
    ("The CAD session hosts.", "Die CAD-Session-Hosts."),
    ("The NVIDIA license.", "Die NVIDIA-Lizenz."),
    ("Total 1,340.15 EUR.", "Summe 1.340,15 EUR."),
    ("Total 1,340.15 EUR.", "Summe 1 340,15 EUR."),
    ("Total 1.340,15 EUR.", "Total 1,340.15 EUR."),
    ("It costs 2.5 EUR and 1,000 GB.", "Es kostet 2,5 EUR und 1.000 GB."),
    ("Region eu-de-1.", "Region eu-de-1."),
])
def test_the_same_fact_in_the_form_of_the_other_language_passes(before, after):
    assert differ(before, after) == []


def test_the_four_real_cases_of_the_task_pass():
    english = "Term 12-month on host g7v from 2026-10-21, column EUR/month."
    german = "Laufzeit 12 Monate auf g7v-Host ab 21.10.2026, Spalte EUR/Monat."
    assert differ(english, german) == []


@pytest.mark.parametrize("before,after,kind", [
    ("A term of 12 months.", "Eine Laufzeit von 24 Monaten.", "number"),
    ("A term of 12 months.", "Eine Laufzeit von 12 Wochen.", "number"),
    ("A term of 12 months.", "Eine Laufzeit von 12.", "number"),
    ("A 12-month term.", "Eine Laufzeit von 24 Monate.", "number"),
    ("The column is EUR/month.", "Die Spalte ist EUR/Tag.", "identifier"),
    ("The column is EUR/month.", "Die Spalte ist EUR.", "identifier"),
    ("The date is 2026-10-21.", "Das Datum ist 22.10.2026.", "number"),
    ("The date is 2026-10-21.", "Das Datum ist 21.11.2026.", "number"),
    ("The date is 2026-10-21.", "Das Datum ist 21.10.2027.", "number"),
    ("The date is 2026-10-11.", "Das Datum ist 10.11.2026.", "number"),
    ("The date is 2026-10-21.", "Das Datum ist 2026.", "number"),
    ("The host is g7v.", "Der g7-Host.", "identifier"),
    ("The host is g7v.", "Der Host.", "identifier"),
    ("The CAD session hosts.", "Die Session-Hosts.", "identifier"),
    ("The NVIDIA license.", "Die Lizenz.", "identifier"),
    ("The NVIDIA license.", "Die AMD-Lizenz.", "identifier"),
    ("Total 1,340.15 EUR.", "Summe 1.340,16 EUR.", "number"),
    ("Total 1,340.15 EUR.", "Summe 1.340,51 EUR.", "number"),
    ("Total 1,340.15 EUR.", "Summe 13.401,5 EUR.", "number"),
    ("It has 100 Mbit/s.", "Es hat 10 Mbit/s.", "number"),
    ("It has 4 vCPU.", "Es hat 8 vCPU.", "number"),
    ("It has 2 TB.", "Es hat 2 GB.", "number"),
    ("Region eu-de-1.", "Region eu-de-2.", "identifier"),
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
    assert not all(k.same for k in writing.keep("The term is 12-month.", "The term is 12 months."))
