"""Ask 2026-11-27-tcp-s2z5-4: tcp-NNNNN and test-NNNNN are one tenant (B-097), so `awb cost billed --tenant
tcp-NNNNN` reads the cache folder that carries the old name, and the owner pull finds the token stored under it.
Invented rows only, the fixtures of tests/test_billed.py."""
import datetime as dt
import json
from types import SimpleNamespace

from awb.tcp import billed, tenants
from tests.test_billed import env, owner_args, row  # noqa: F401  (env is a fixture)


def _window(monkeypatch):
    monkeypatch.setattr(tenants, "canonical", lambda alias, *a, **k: alias)


def test_reader_takes_the_new_name_of_a_tenant_cached_under_its_old_name(env, capsys, monkeypatch):
    _window(monkeypatch)
    (env_store := tenants.Store().root / "awb" / "fd" / "test-10491.gpg").touch()
    env.rows = [row()]
    billed.pull(env.p, "test-10491", "2026-11")
    assert billed.main(["billed", "--tenant", "tcp-10491", "--month", "2026-11", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)[0]
    assert data["alias"] == "test-10491" and data["total"] == "0.10"
    assert env_store.exists()


def test_owner_pull_takes_the_new_name_and_uses_the_token_of_the_old_one(env, capsys, monkeypatch):
    _window(monkeypatch)
    (tenants.Store().root / "awb" / "fd" / "test-10491.gpg").touch()
    env.rows = [row()]
    assert billed.owner_command(env.p, owner_args(alias="tcp-10491")) == 0
    assert capsys.readouterr().out.startswith("test-10491: 2026-11 0.10 EUR")
    assert (tenants.folder(env.p, "test-10491") / "billed-2026-11.json").exists()


def test_an_unknown_tenant_still_has_no_cache(env, capsys, monkeypatch):
    _window(monkeypatch)
    assert billed.main(["billed", "--tenant", "tcp-10777", "--month", "2026-11"]) == 0
    assert "no cache for 2026-11" in capsys.readouterr().out
