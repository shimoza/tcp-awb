"""Rates of the live price API and dates in table cells are no phone numbers; real phone numbers still are
(notice tcp-renz 2026-11-19)."""
import pytest

from awb.patterns import find_structured


def phones(text: str) -> list[str]:
    return [text[s.start:s.end] for s in find_structured(text) if s.cls == "phone"]


@pytest.mark.parametrize("text", [
    "| Flavor | Rate |\n|---|---|\n| s3.large.2 | 0.123456 |\n| s3.xlarge.2 | 0.246912 |\n",
    "| Flavor | Rate |\n|---|---|\n| s3.large.2 | 0.123456 | 0.234567 |\n",
    "| a | 0.123456 |\n| b | 0.012345 |\n",
    "| Flavor | On demand | Reserved |\n|---|---|---|\n| s3.large.2 | 0.1234 | 0.98765 |\n",
    "rate 0.123456 per hour",
    "s3.large.2 costs 0.123456 EUR",
    "0.123456 /h",
    "0.12345 €",
    "the hourly rate is 0.023456",
])
def test_rates_are_no_phone(text):
    assert phones(text) == []


@pytest.mark.parametrize("text", [
    "| 2026-11-19 | 2026-12-01 |",
    "| From | To | Rate |\n|---|---|---|\n| 2026-11-19 | 2026-12-01 | 0.123456 |\n",
    "| 2026-11-19 | 2026-12-01 | 0.123456 |",
    "| 01 | 2026-11-19 |",
    "| 19.11.2026 | 01.12.2026 |",
    "| 19.11.2026 | 01.12.2026 | 0.123456 |",
    "valid 2026-11-19 2026-12-01",
    "valid 19.11.2026  01.12.2026",
    "| 1.12.2026 | 05.12.2026 |",
])
def test_dates_are_no_phone(text):
    assert phones(text) == []


@pytest.mark.parametrize("text,phone", [
    ("Tel +49 30 1234567", "+49 30 1234567"),
    ("call 030 1234567 today", "030 1234567"),
    ("mobile 0171-1234567", "0171-1234567"),
    ("| Tel | 030 1234567 |", "030 1234567"),
    ("| Name | Phone |\n|---|---|\n| A | 0171-1234567 |\n", "0171-1234567"),
    ("| 2026-11-19 | 030 1234567 |", "030 1234567"),
    ("2026-11-19 0171-1234567", "0171-1234567"),
    ("| Rate | Phone |\n|---|---|\n| 0.123456 | 030 1234567 |\n", "030 1234567"),
])
def test_real_phones_still_found(text, phone):
    assert phones(text) == [phone]


def test_rate_column_with_a_phone_is_no_rate_column():
    # a cell of that shape in a column that holds a phone number is not let through by the column rule
    text = "| a | b |\n|---|---|\n| x | 0.123456 |\n| y | 030 1234567 |\n"
    assert "030 1234567" in phones(text)
    assert "0.123456" in phones(text)
