"""A first name inside a host name and the account of a code-host address are wiped (ask 2026-11-27-tcp-7c7b-3).

The import of a code tree let two name forms through: a first name as one label or one dash part of a host name
(`NAME-dev`, `NAME-dev.HOST`) and the owner segment of `github.com/ACCOUNT/repo`. The entry keeps host names
and web addresses as written (DMZ 1), so only the name piece becomes a token; a backtick span is read too. Common words
stay (B-117): a first name that is also a word counts only next to a host role word or a host number. The names are
invented (tests/fixtures.py).
"""
from __future__ import annotations

from awb.intake.engine import _Engine
from awb.intake.wipe import runstate as rs
from tests import fixtures as fx

FIRST = fx.HOST_FIRST.lower()
WORD_FIRST = fx.HOST_WORD_FIRST.lower()
ACCOUNT = fx.CODE_HOST_ACCOUNT


def _wipe(*texts: str) -> list[str]:
    """Every text through the engine of the entry, learned like one import run, then sanitised."""
    engine = _Engine([], entry=True)
    for i, t in enumerate(texts):
        engine.detect("F-TEST.%d detect" % i, t)
    return [engine.sanitize(t, "F-TEST.%d output" % i)[0] for i, t in enumerate(texts)]


def test_a_first_name_as_a_dash_part_of_a_bare_host_name_becomes_a_token():
    (out,) = _wipe("Log in to `%s-dev` as root, then on %s-dev run the build.\n" % (FIRST, FIRST))
    assert FIRST not in out.lower()
    assert out.count("[person 1]-dev") == 2


def test_a_first_name_inside_a_dotted_host_and_a_web_address_becomes_a_token_and_the_rest_stays():
    host = fx.LEDGER_LINK_HOST
    text = ("The runner is %s-dev.%s and the page https://build.%s.%s/run/7.\n" % (FIRST, host, FIRST, host))
    (out,) = _wipe(text)
    assert FIRST not in out.lower()
    assert "[person 1]-dev.%s" % host in out
    assert "https://build.[person 1].%s/run/7" % host in out


def test_the_first_name_of_a_host_is_learned_and_wiped_where_it_stands_as_a_name():
    out = _wipe("Host: %s-dev\n" % FIRST, "Ask %s about the firewall.\n" % fx.HOST_FIRST)
    assert fx.HOST_FIRST not in out[1]


def test_the_account_of_a_code_host_address_becomes_a_token():
    text = ("Clone https://github.com/%s/tender-tool.git, the mirror gitlab.com/%s/infra and "
            "`https://codeberg.org/%s/notes`.\n" % (ACCOUNT, ACCOUNT, ACCOUNT))
    (out,) = _wipe(text)
    assert ACCOUNT not in out
    assert "https://github.com/[name 1]/tender-tool.git" in out
    assert "gitlab.com/[name 1]/infra" in out


def test_a_public_organisation_and_a_page_of_the_code_host_stay():
    text = ("Use github.com/hashicorp/terraform, github.com/opentelekomcloud/terraform-provider-opentelekomcloud, "
            "https://github.com/features/actions and https://gitlab.com/explore.\n")
    (out,) = _wipe(text)
    assert out == text


def test_common_words_in_dash_compounds_stay():
    text = ("Set max-age and max-width, the file deploy-%s.py, the month jan-2026, mark-down and "
            "will-change stay as they are.\n" % FIRST)
    (out,) = _wipe(text)
    assert "max-age" in out and "max-width" in out and "jan-2026" in out and "mark-down" in out
    assert "will-change" in out
    assert "deploy-%s.py" % FIRST in out, "a file name is no host"


def test_a_first_name_that_is_a_word_counts_next_to_a_host_role_word_only():
    (out,) = _wipe("Hosts: %s-dev, %s-01 and %s-pipeline.\n" % (WORD_FIRST, WORD_FIRST, WORD_FIRST))
    assert "%s-dev" % WORD_FIRST not in out and "%s-01" % WORD_FIRST not in out
    assert "%s-pipeline" % WORD_FIRST in out


def test_the_rule_is_named_in_the_report_words():
    assert rs.RULE_LABELS["_host_name_candidates"] == "first name in a host name or account of a code host"
