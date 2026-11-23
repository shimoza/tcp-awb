"""The keep list in an encrypted vault takes a second phrase (tcp-ui5o, 2026-11-23): the owner's
`awb register keep --from` answered "the vault daemon refused the request". seal_file keeps an existing <file>.gpg
(the recovery of an interrupted seal) and refuses when it does not decrypt to the new plaintext, so every keep after
the first one failed against a real daemon; the fake daemon of the older tests overwrote the .gpg and hid it. The
new list is now sealed under a fresh name and moved over the old sealed one. Every phrase is invented."""
from __future__ import annotations

from awb import intake
from tests import fixtures as fx
from tests.test_intake import sealed_vault  # noqa: F401


def test_ui5o_a_second_and_a_third_keep_go_through_a_real_daemon(sealed_vault):
    p = sealed_vault
    assert intake.keep_phrase(p, fx.PLANTED_CANDIDATE) == (True, 1)
    assert intake.keep_phrases(p, ["zentrix vorlauf", "alpha test tool"]) == (2, 3)
    assert intake.keep_phrase(p, "beta term") == (True, 4)
    assert intake.load_keep(p) == [fx.PLANTED_CANDIDATE, "zentrix vorlauf", "alpha test tool", "beta term"]
    assert not p.keep_list.exists() and (p.vault / "keep.tsv.gpg").is_file()
    assert sorted(x.name for x in p.vault.iterdir() if x.name.startswith("keep")) == ["keep.tsv.gpg"]
    assert b"beta term" not in (p.vault / "keep.tsv.gpg").read_bytes()
