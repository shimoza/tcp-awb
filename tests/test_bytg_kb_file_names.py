"""awb kb add and amend (tcp-bytg ask 2, 2026-11-21): a statement that names a Windows log file such as
setupapi.dev.log was refused as a web address (the `.dev` inside reads as a domain), and the refusal for a similar
entry came before the list of similar entries, where it was easy to miss. A file name now passes; a real web address
in the statement is still refused; the refusal and "nothing was added" are the last lines."""
from __future__ import annotations

from awb import kb
from tests.test_kb import add_argv, run  # noqa: F401

S_LOG = "Windows writes the driver installs of a server to setupapi.dev.log in the INF folder of the system disk."


def test_bytg_a_log_file_name_passes_the_kb_add(home, capsys):
    code, out, err = run(add_argv(S_LOG, tag="ims"), capsys)
    assert code == 0 and out.startswith("added KB-"), (out, err)


def test_bytg_a_web_address_in_a_statement_is_still_refused(home, capsys):
    code, out, err = run(add_argv("The driver list of a server is at https://setupapi.dev/list for each image.",
                                  tag="ims"), capsys)
    assert code == 1 and "name check" in err


def test_bytg_the_refusal_of_a_similar_entry_comes_last(home, capsys):
    assert run(add_argv(S_LOG, tag="ims"), capsys)[0] == 0
    code, out, err = run(add_argv(S_LOG.replace("server", "machine"), tag="ims"), capsys)
    assert code == 1
    assert err.strip().splitlines()[-1] == "awb kb add: nothing was added"
    assert "too similar" in err.strip().splitlines()[-2]


def test_bytg_only_a_known_suffix_right_after_the_hit_makes_a_file_name():
    text = "see setupapi.dev.log and setupapi.dev.example and setupapi.dev"
    hits = [{"start": 4, "length": 12, "cls": "url"}, {"start": 26, "length": 12, "cls": "url"},
            {"start": 51, "length": 12, "cls": "url"}]
    assert kb._without_file_names(text, hits) == hits[1:]
