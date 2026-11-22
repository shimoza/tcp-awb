"""awb service update in an installed release, whose rules folder is read-only, says where to run it."""
from __future__ import annotations

from awb.tcp import services


def test_update_in_a_read_only_release_names_the_checkout_and_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(services.os, "access", lambda path, mode: False)
    called = []
    monkeypatch.setattr(services, "update", lambda *a, **k: called.append(1))
    assert services.main(["update"]) == 2
    err = capsys.readouterr().err
    assert "read-only" in err and "repository checkout" in err and "Traceback" not in err
    assert called == []
