"""An import into a project whose customer was issued without a form (a fully anonymous project) asks for no form."""

from __future__ import annotations

import pytest

from awb import bucket, config, importcmd, projects, register, start

GOAL = "move two app clusters to managed k8s"


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


def _reader(lines):
    it = iter(lines)
    return lambda: next(it, "")


def test_an_import_for_a_customer_issued_without_a_form_asks_for_none(home, capsys, tmp_path):
    assert start.start(home, "project", GOAL, "new", read=_reader(["\n"]), tty=True) == 0
    (pr,) = projects.load(home)
    capsys.readouterr()
    f = tmp_path / "notes.md"
    f.write_text("Enable Credential Guard on a Windows Server 2022 guest.\n", encoding="utf-8")

    def no_read():
        raise AssertionError("the import asked for a form")

    assert importcmd.run(home, pr.code, [f], read=no_read, tty=True) == 0
    out = "".join(capsys.readouterr())
    assert "%s was issued without a form" % pr.customer in out
    assert "copies written: 1" in out
    assert not [e for e in register.load(home.register) if e.code == pr.customer]


def test_a_customer_never_issued_is_still_asked_for_its_forms(home, tmp_path):
    assert start.start(home, "project", GOAL, "new", read=_reader(["\n"]), tty=True) == 0
    (pr,) = projects.load(home)
    (home.outbox / pr.customer).rmdir()
    f = tmp_path / "notes.md"
    f.write_text("A note.\n", encoding="utf-8")
    with pytest.raises(importcmd.ImportRefused, match="no form was typed"):
        importcmd.run(home, pr.code, [f], read=_reader(["\n"]), tty=True)
