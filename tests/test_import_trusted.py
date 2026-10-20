"""`awb import --trusted`: the owner vouches that the files hold no name, so no name is guessed; the registered
forms, mail addresses and the secret check still apply."""

from __future__ import annotations

import pytest

from awb import bucket, config, importcmd, projects, start
from tests import fixtures as fx

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


def _copy(home, cust) -> str:
    files = [x for x in (home.outbox / cust).rglob("F-*") if x.is_file()]
    assert files, sorted(x.name for x in (home.outbox / cust).rglob("*"))
    return "\n".join(x.read_text(encoding="utf-8", errors="replace") for x in files)


def _project(home, capsys):
    assert start.start(home, "project", GOAL, "new", read=_reader(["\n"]), tty=True) == 0
    (pr,) = projects.load(home)
    capsys.readouterr()
    return pr


def test_trusted_guesses_no_name_and_still_replaces_a_registered_form_and_a_mail_address(home, capsys, tmp_path):
    pr = _project(home, capsys)
    f = tmp_path / "notes.md"
    f.write_text("Outcome\n\nWorked on Sapphire Rapids with KVM in %s. Scorecard: APPEARED.\n"
                 "Microsoft and Intel. Mail: quorvane.halbrecht@example.org\n" % fx.CUSTOMER_FORMS[0], encoding="utf-8")
    assert importcmd.run(home, pr.code, [f], tty=True, trusted=True) == 0
    out = "".join(capsys.readouterr())
    assert "trusted: no name is guessed" in out
    copy = _copy(home, pr.customer)
    for word in ("Outcome", "Sapphire Rapids", "KVM", "Scorecard", "APPEARED"):
        assert word in copy, word
    assert fx.CUSTOMER_FORMS[0] not in copy, "a registered form is still replaced"
    assert "Microsoft" in copy and "Intel" in copy
    assert "quorvane.halbrecht@example.org" not in copy, "a mail address is still wiped"


def test_without_trusted_the_guessing_still_runs(home, capsys, tmp_path):
    pr = _project(home, capsys)
    f = tmp_path / "notes.md"
    f.write_text("The pilot ran with Quorvane Halbrecht and Sapphire Rapids.\n", encoding="utf-8")
    assert importcmd.run(home, pr.code, [f], tty=True) == 0
    assert "Quorvane Halbrecht" not in _copy(home, pr.customer)
