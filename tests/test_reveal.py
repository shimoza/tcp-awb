"""awb/reveal.py: the real names back into a finished text, on the owner side only."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

import awb
from awb import cli, config, projects, register, reveal
from tests import fixtures as fx

FULL = fx.CUSTOMER_FORMS[0]
ROOT = Path(awb.__file__).resolve().parent.parent


def test_codes_become_their_first_active_form(register_path):
    entries = register.load(register_path)
    text = "Offer for %s, contact %s, old partner PART-RET2, mail MAIL-AB2C." % (fx.CUSTOMER_CODE, fx.PERSON_CODE)
    named, stats = reveal.reveal_text(text, entries)
    assert FULL in named and fx.PERSON_FORMS[0] in named
    assert "PART-RET2" in named and "MAIL-AB2C" in named
    assert stats == {"replaced": 2, "retired": 1, "unknown": 1}


def test_the_named_text_goes_to_a_private_file(home, tmp_path):
    out = tmp_path / "private" / "offer.md"
    out.parent.mkdir()
    stats = reveal.write_named("for %s" % fx.CUSTOMER_CODE, out, home)
    assert out.read_text(encoding="utf-8") == "for %s" % FULL and stats["replaced"] == 1
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    with pytest.raises(reveal.RevealError, match="exists"):
        reveal.write_named("again", out, home)
    reveal.write_named("again %s" % fx.CUSTOMER_CODE, out, home, replace=True)
    assert out.read_text(encoding="utf-8") == "again %s" % FULL


@pytest.mark.parametrize("where", ["shared", "kb"])
def test_never_where_a_session_reads(home, where):
    folder = getattr(home, where)
    folder.mkdir(parents=True, exist_ok=True)
    with pytest.raises(reveal.RevealError, match="working session"):
        reveal.write_named("for %s" % fx.CUSTOMER_CODE, folder / "x.md", home)
    assert not (folder / "x.md").exists()


def test_never_into_a_project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    pr = projects.spawn(home, "engagement", "move two app clusters to managed k8s", fx.CUSTOMER_CODE, register_path)
    target = Path(pr.path) / "deliverables" / "named.md"
    with pytest.raises(reveal.RevealError, match="working session"):
        reveal.write_named("for %s" % fx.CUSTOMER_CODE, target, home)
    assert not target.exists()


def test_the_work_user_is_refused(home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    with pytest.raises(reveal.RevealError, match="owner side"):
        reveal.write_named("for %s" % fx.CUSTOMER_CODE, tmp_path / "x.md", home)
    assert not (tmp_path / "x.md").exists()


def test_the_command_prints_no_name(home, tmp_path, capsys):
    src = tmp_path / "draft.md"
    src.write_text("offer for %s\n" % fx.CUSTOMER_CODE, encoding="utf-8")
    out = tmp_path / "named.md"
    assert cli.main(["reveal", str(src), "--out", str(out)]) == 0
    printed = capsys.readouterr()
    fx.assert_no_fixture_name(printed.out + printed.err, "the output of awb reveal")
    assert "1 codes replaced" in printed.out
    assert out.read_text(encoding="utf-8") == "offer for %s\n" % FULL
    assert cli.main(["reveal", str(src), "--out", str(out)]) == 2
