"""Ask 2026-11-25-tcp-bytg-1, changed by task B-080 (the owner's word of 2026-11-25): WinLab runs no secret check of a
command, so a ConvertTo-SecureString -AsPlainText with a typed password is sent like every other command. The guards
of the machine itself stay (guard_text): an empty text, a {{secret:NAME}} reference and the machine's own state."""
import pytest

from awb import winlab
from awb import winlabsession as w
from tests.test_hooks import project  # noqa: F401 - the fixture of lab
from tests.test_wl_session import MACHINE, lab  # noqa: F401 - the fixture


def test_a_quoted_description_after_the_bracket_is_no_password():
    line = ("New-LocalUser -Name vbrtest -Password (ConvertTo-SecureString $pw -AsPlainText -Force) "
            "-PasswordNeverExpires -Description 'temporary user for the lab test' | Out-Null")
    w.guard_text(line)


def test_a_quoted_literal_inside_the_bracket_is_still_a_password(lab):
    line = ("New-LocalUser -Name vbrtest -Password (ConvertTo-SecureString 'Summer2026!' -AsPlainText -Force) "
            "-Description 'temporary user'")
    w.guard_text(line)                    # a typed password is no longer refused: no secret check of a command runs
    w.run(MACHINE, line)
    assert line in lab.sent[-1]
    assert not hasattr(w, "_PS_PLAIN_RE")


def test_a_variable_on_its_own_line_is_no_password():
    w.guard_text("$sec = ConvertTo-SecureString $pw -AsPlainText -Force")
    with pytest.raises(winlab.WinlabError, match="is empty"):
        w.guard_text("  \n")
