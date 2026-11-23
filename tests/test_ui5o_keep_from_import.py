"""awb register keep --from tcp-xxxx --token "name 2" (tcp-ui5o ask 2, 2026-11-23): a public term the wipe took for
a name is kept by the token the session sees, without the owner typing or seeing it; awb import --redo then writes
the copy with the term. A register code or a data code never comes out this way. Every name is an invented fixture
name."""
from __future__ import annotations

import re

import pytest

from awb import candidates, cli, importcmd, intake
from tests import fixtures as fx
from tests.test_import import _copies, _files, _lines, _no_name, owner_terminal, project  # noqa: F401


def _token_of_candidate(home) -> str:
    text = next(iter(_copies(home).values()))
    m = re.search(r"das Angebot der (\[(?:name|company|person|place) \d+\])", text)
    assert m, text
    return m.group(1)


def test_ui5o_a_wiped_term_is_kept_and_the_redo_writes_it(home, project, capsys):
    _files(home.inbox)
    importcmd.run(home, project.code, [])
    tok = _token_of_candidate(home)
    capsys.readouterr()
    assert cli.main(["register", "keep", "--from", project.code, "--token", tok.strip("[]")]) == 0
    out = _lines(capsys)
    assert "kept 1 new phrase(s)" in out and "awb import %s --redo" % project.code in out
    _no_name(out)
    assert importcmd.run(home, project.code, [], redo=True) == 0
    after = _copies(home)
    assert all(fx.PLANTED_CANDIDATE in t for t in after.values())
    assert all(fx.PLANTED_PERSON not in t for t in after.values()), "only the kept term comes back"


def test_ui5o_an_unknown_mark_is_named_and_nothing_else_is_said(home, project, capsys):
    _files(home.inbox, 1)
    importcmd.run(home, project.code, [])
    capsys.readouterr()
    assert cli.main(["register", "keep", "--from", project.code, "--token", "name 99"]) == 1
    out = _lines(capsys)
    assert "[name 99]" in out and "kept 0 new phrase(s)" in out
    _no_name(out)
    assert intake.load_keep(home) == []


@pytest.mark.parametrize("token", [fx.CUSTOMER_CODE, "URL-AB2C", "name", "boss 1", ""])
def test_ui5o_a_register_code_a_data_code_or_a_broken_mark_is_refused(home, project, token):
    _files(home.inbox, 1)
    importcmd.run(home, project.code, [])
    assert cli.main(["register", "keep", "--from", project.code, "--token", token]) != 0
    assert intake.load_keep(home) == []


def test_ui5o_only_the_wiped_section_gives_values():
    report = "\n".join([
        "## Replacements", "", "| token | class | value | count | where and positions |", "| --- | --- | --- | --- | --- |",
        "| %s | CUST | registered form | 1 | x |" % fx.CUSTOMER_CODE,
        "| [name 1] | unknown | not from here | 1 | x |", "",
        "## Wiped", "", "| value | class | token | rule | count | where and positions |",
        "| --- | --- | --- | --- | --- | --- |",
        "| alpha \\| beta | unknown | [name 1] | run | 1 | x |",
        "| %s | CUST | %s | run | 1 | x |" % ("other form", fx.CUSTOMER_CODE), "",
    ])
    assert candidates.wiped_values(report) == {"[name 1]": ["alpha | beta"]}
