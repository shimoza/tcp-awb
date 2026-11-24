"""An ask project hands a result out with awb xchg put (ask 2026-11-24-tcp-s2z5-1, the owner's request).

Inbound stays refused (inbox take, xchg list as before); only the outbound put runs, and it checks the file as for
every project (here the put itself is a recorder).
"""
from __future__ import annotations

from pathlib import Path

from awb.tcp import xchg
from tests.test_ask_kind import granted, no_project_dir  # noqa: F401


def test_s2z5_an_ask_project_puts_a_result_out_and_still_refuses_inbound(home, monkeypatch, capsys):
    folder = granted(home)
    monkeypatch.chdir(folder)
    (folder / "draft.md").write_text("a draft\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(xchg, "put", lambda path, *a, **k: seen.append(Path(path).name) or "put draft.md")
    assert xchg.main_xchg(["put", "draft.md"]) == 0
    assert seen == ["draft.md"]
    capsys.readouterr()
    assert xchg.main_inbox(["take", "the", "offer"]) == 1
    assert "ask project" in capsys.readouterr().err
    assert xchg.main_xchg(["list"]) == 1
