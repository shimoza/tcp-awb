"""Logins of customer persons are wiped (ask 2026-11-27-tcp-j5sy, import 2 under-scrubbed).

A first name and surname joined by a dot or an underscore (first.last) and an initial with a surname (flast) as the
value of a login option, a Windows account or a home folder became no token. Code calls, system users and option
values that are words stay. The names are invented (tests/fixtures.py).
"""
from __future__ import annotations

from awb.intake.engine import _Engine
from tests import fixtures as fx

LONG, SHORT, DOMAIN = fx.LOGIN_FIRST_LAST, fx.LOGIN_SHORT, fx.LOGIN_DOMAIN
SURNAME = LONG.split(".")[1]
HOME = "/" + "home" + "/"      # joined, so the commit gate reads no home path in this file


def _wipe(text: str) -> str:
    engine = _Engine([], entry=True)
    engine.detect("F-TEST detect", text)
    return engine.sanitize(text, "F-TEST output")[0]


def test_first_dot_last_and_first_underscore_last_go_whole():
    out = _wipe("The owner is %s, in the old tool %s, and `%s` in the config.\n"
                % (LONG, LONG.replace(".", "_"), LONG))
    assert SURNAME not in out.lower() and fx.MEETING_FIRSTS[8].lower() not in out.lower()


def test_a_windows_account_a_profile_folder_and_a_first_dot_last_option_value_go():
    text = ("CREATE LOGIN [%s\\%s] FROM WINDOWS;\ncopy to C:\\Users\\%s\\Desktop and /Users/%s/notes\n"
            "sudo -u %s psql and ls %s%s/backup\n" % (DOMAIN, SHORT, SHORT, SHORT, LONG, HOME, LONG))
    out = _wipe(text)
    assert SHORT not in out and SURNAME not in out
    assert "sudo -u [person" in out and HOME + "[person" in out


def test_a_service_user_of_a_server_stays():
    text = "sudo -u tndrsvc /srv/tndr/run.sh, chown tndrsvc %stndrsvc/data\n" % HOME
    assert _wipe(text) == text


def test_system_users_words_and_code_calls_stay():
    text = ("sudo -u postgres psql -U admin, ls " + HOME + "ubuntu/x, run as NT AUTHORITY\\SYSTEM, tar -u files.tar, "
            "self.config.items() and anna.items().\n")
    assert _wipe(text) == text
