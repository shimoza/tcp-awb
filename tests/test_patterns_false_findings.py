"""Three false findings of the name check (UX17 of 2026-11-11): PowerShell member access read as a bank code or a
web address, a price with six decimals read as a phone number and `<file>.de.md` refused by `awb review init`.
Every example is an invented value."""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import check, patterns, review

CODE = "tcp-q7m4"


def hits(text: str, code: bool = False) -> list[tuple[str, str]]:
    return [(s.cls, text[s.start:s.end]) for s in patterns.find_structured(text, code=code)]


# --------------------------------------------------------------------------- (1) PowerShell members

_PS = ("$state = $Reply.Status\n"
       "$box = $Obj.HOSTNM01\n"
       "$zone = $cfg.Server.local\n"
       "$res = $o.Result.com\n"
       "$head = (Get-Date).Server.com\n")


def test_powershell_files_are_read_in_the_code_view():
    assert patterns.is_code_file("deploy.ps1") and patterns.is_code_file("Tools.PSM1")


def test_a_token_after_a_dollar_or_a_dot_is_no_bank_code_in_the_code_view():
    text = "$box = $SERVERNAMES\n$a = $Obj.HOSTNM01\n"
    assert [t for c, t in hits(text) if c == "bic"] == ["SERVERNAMES", "HOSTNM01"]
    assert [h for h in hits(text, code=True) if h[0] == "bic"] == []


def test_a_member_after_a_dollar_or_a_dot_is_no_web_address_in_the_code_view():
    assert any(c == "url" for c, _ in hits(_PS))
    assert [h for h in hits(_PS, code=True) if h[0] == "url"] == []


def test_a_ps1_file_is_checked_in_the_code_view(register_path, tmp_path):
    ps1 = tmp_path / "deploy.ps1"
    ps1.write_text(_PS + "$box = $Obj.HOSTNM01\n", encoding="utf-8")
    assert check.check_file(ps1, register_path, code=True) == []
    assert check.check_file(ps1, register_path) != []


def test_the_code_view_keeps_a_bank_code_that_stands_alone():
    text = "payee = HOSTNM01\nBIC: DEUTDEFF500\n"
    assert [h for h in hits(text, code=True) if h[0] == "bic"] == [h for h in hits(text) if h[0] == "bic"] != []


# --------------------------------------------------------------------------- (2) prices are no phones


@pytest.mark.parametrize("text", [
    "Price 0.123456 EUR per hour",
    "The unit price is 0.012345 per GB",
    "unit_price=0.0123456",
    "Preis: 0,123456 EUR je GB",
    "| Flavor | Price per hour |\n| s3.large.2 | 0.034567 |\n| s3.large.4 | 0.045678 |\n",
    "| Price | 0.012345 |\n| 0.034567 | 0.045678 |\n",
])
def test_a_price_with_decimals_is_no_phone(text):
    assert [h for h in hits(text) if h[0] == "phone"] == []


@pytest.mark.parametrize("text", [
    "Tel. 0.123456",
    "0.123456",
    "Call 030 123456 for the price of 0.12 EUR",
    "| Name | Number |\n| a | 0.034567 |\n",
])
def test_a_number_without_a_price_near_it_is_still_a_phone(text):
    assert [h for h in hits(text) if h[0] == "phone"] != []


def test_a_phone_with_a_country_code_next_to_a_price_is_still_a_phone():
    assert ("phone", "+49 30 1234567") in hits("EUR 12 for a call to +49 30 1234567")


# --------------------------------------------------------------------------- (3) review init and translations


@pytest.fixture
def project(home) -> Path:
    root = home.projects_root / CODE
    for sub in ("deliverables", "reviews", "evidence", "input"):
        (root / sub).mkdir(parents=True)
    (root / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- platform: tcp\n- mode: sealed\n" % (CODE, CODE),
                                   encoding="utf-8")
    return root


@pytest.mark.parametrize("lang", ["de", "en", "ru"])
def test_review_init_accepts_the_name_awb_write_translate_writes(project, tmp_path, lang):
    d = project / "deliverables" / ("offer.%s.md" % lang)
    d.write_text("# Offer\n\nThe backup runs every night.\n", encoding="utf-8")
    request = tmp_path / "request.txt"
    request.write_text("Check the offer for the platform team.\n", encoding="utf-8")
    target = review.init(d, request, 2, 400)
    assert target.is_file()


def test_review_init_still_refuses_a_name_in_the_stem_of_a_translation(project, tmp_path):
    from tests import fixtures

    d = project / "deliverables" / ("%s.de.md" % fixtures.CUSTOMER_FORMS[0].replace(" ", "-"))
    d.write_text("# Offer\n\nThe backup runs every night.\n", encoding="utf-8")
    request = tmp_path / "request.txt"
    request.write_text("Check the offer for the platform team.\n", encoding="utf-8")
    with pytest.raises(review.Refused):
        review.init(d, request, 2, 400)
