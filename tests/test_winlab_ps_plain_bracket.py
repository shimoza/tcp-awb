"""Ask 2026-11-25-tcp-bytg-1: a quoted -Description after a bracketed ConvertTo-SecureString of a variable is no
password; a quoted literal inside the bracket still is."""
from awb import winlabsession as w


def test_a_quoted_description_after_the_bracket_is_no_password():
    line = ("New-LocalUser -Name vbrtest -Password (ConvertTo-SecureString $pw -AsPlainText -Force) "
            "-PasswordNeverExpires -Description 'temporary user for the lab test' | Out-Null")
    assert w._PS_PLAIN_RE.findall(line) == []


def test_a_quoted_literal_inside_the_bracket_is_still_a_password():
    line = ("New-LocalUser -Name vbrtest -Password (ConvertTo-SecureString 'Summer2026!' -AsPlainText -Force) "
            "-Description 'temporary user'")
    assert len(w._PS_PLAIN_RE.findall(line)) == 1


def test_a_variable_on_its_own_line_is_no_password():
    assert w._PS_PLAIN_RE.findall("$sec = ConvertTo-SecureString $pw -AsPlainText -Force") == []
