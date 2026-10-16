"""P7, P14 and P15 of F3: lab defaults from SCOPE.md, awb unlock and awb without arguments.

Throw-away folders and a throw-away Workbench; every name is an invented alias or code of the fixtures.
"""
from __future__ import annotations

import pytest

from awb import cli, config, projects
from awb.tcp import lab
from tests import fixtures

CODE = "tcp-ab2c"
TENANT = "test-1"
GOAL = "move two app clusters to managed k8s now"


@pytest.fixture
def folder(tmp_path, monkeypatch):
    root = tmp_path / CODE
    root.mkdir()
    (root / "SCOPE.md").write_text("# Scope\n\n- code: %s\n- tenant: %s\n" % (CODE, TENANT), encoding="utf-8")
    (root / "evidence").mkdir()
    monkeypatch.chdir(root / "evidence")
    return root


@pytest.fixture
def seen(monkeypatch):
    calls = []
    monkeypatch.setattr(lab, "run", lambda name, project, tenant, given, **kw: calls.append((name, project, tenant)) or 0)
    return calls


# --- P7


def test_scope_defaults_read_the_code_and_the_tenant_line(folder):
    assert lab.scope_defaults() == (CODE, TENANT)


def test_lab_run_takes_project_and_tenant_from_the_folder(folder, seen):
    assert lab.main(["run", "widget-check"]) == 0
    assert seen == [("widget-check", CODE, TENANT)]


def test_the_flags_still_win(folder, seen):
    assert lab.main(["run", "widget-check", "--project", "tcp-q7m4", "--tenant", "test-2"]) == 0
    assert lab.main(["run", "widget-check", "--tenant", "test-2"]) == 0
    assert seen == [("widget-check", "tcp-q7m4", "test-2"), ("widget-check", CODE, "test-2")]


def test_a_folder_without_a_tenant_line_stops_with_one_line(folder, seen, capsys):
    (folder / "SCOPE.md").write_text("# Scope\n\n- code: %s\n" % CODE, encoding="utf-8")
    assert lab.main(["run", "widget-check"]) == lab.EXIT_STOPPED
    assert "give --tenant" in capsys.readouterr().err and seen == []


def test_outside_a_project_both_flags_are_asked_for(tmp_path, monkeypatch, seen, capsys):
    monkeypatch.chdir(tmp_path)
    assert lab.main(["run", "widget-check"]) == lab.EXIT_STOPPED
    assert "give --project" in capsys.readouterr().err and seen == []


def test_spawn_writes_the_tenant_line_only_when_one_is_given(home):
    with_tenant = projects.spawn(home, "project", GOAL, fixtures.CUSTOMER_CODE, home.register, tenant=TENANT)
    without = projects.spawn(home, "project", GOAL, fixtures.CUSTOMER_CODE, home.register)
    assert "- tenant: %s\n" % TENANT in (home.projects_root / with_tenant.code / "SCOPE.md").read_text()
    assert "tenant:" not in (home.projects_root / without.code / "SCOPE.md").read_text()


@pytest.mark.parametrize("kind,alias", [("query", TENANT), ("project", "Test 1")])
def test_spawn_refuses_a_tenant_for_a_query_and_a_bad_alias(home, kind, alias):
    with pytest.raises(projects.ProjectError):
        projects.spawn(home, kind, GOAL, fixtures.CUSTOMER_CODE, home.register, tenant=alias)
    assert not list(home.projects_root.iterdir())


# --- P14


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(config, "work_user", lambda: None)


def _mains(monkeypatch, vault_code=0, keys_code=0):
    from awb import vault
    from awb.tcp import keys

    order = []
    monkeypatch.setattr(vault, "main", lambda argv: order.append(("vault", argv)) or vault_code)
    monkeypatch.setattr(keys, "main", lambda argv: order.append(("keys", argv)) or keys_code)
    return order


def test_unlock_runs_the_vault_then_the_keys_without_any_argument(owner, monkeypatch):
    order = _mains(monkeypatch)
    assert cli.main(["unlock"]) == 0
    assert order == [("vault", ["unlock"]), ("keys", ["unlock"])]


def test_unlock_stops_at_the_first_failure(owner, monkeypatch, capsys):
    order = _mains(monkeypatch, vault_code=2)
    assert cli.main(["unlock"]) == 2
    assert order == [("vault", ["unlock"])] and "stopped at awb vault unlock" in capsys.readouterr().err
    order = _mains(monkeypatch, keys_code=1)
    assert cli.main(["unlock"]) == 2
    assert "stopped at awb keys unlock" in capsys.readouterr().err


def test_unlock_takes_no_passphrase_option(owner):
    assert cli.main(["unlock", "--stdin"]) == 2


def test_unlock_is_for_the_owner(monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    order = _mains(monkeypatch)
    assert cli.main(["unlock"]) == 2 and order == []


# --- P15


def _states(monkeypatch, vault_state, keys_state):
    from awb import vault
    from awb.tcp import keys

    def ping(sock, timeout=10.0):
        if vault_state is None:
            raise vault.VaultUnavailable("none")
        return {"state": vault_state, "since": "2026-10-16T08:00:00Z" if vault_state == "locked" else None}

    monkeypatch.setattr(vault, "ping_state", ping)
    monkeypatch.setattr(keys, "request", lambda sock, obj, timeout=5: {"ok": True, "state": keys_state})


def test_awb_alone_prints_side_locks_and_the_next_owner_item(home, owner, monkeypatch, capsys):
    pr = projects.spawn(home, "project", GOAL, fixtures.CUSTOMER_CODE, home.register)
    open_md = home.projects_root / pr.code / "OPEN.md"
    open_md.write_text(open_md.read_text() + "\n- The owner switches %s to a project.\n" % pr.code)
    _states(monkeypatch, "locked", "unlocked")
    assert cli.main([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "side: owner"
    assert out[1].startswith("vault: locked since 2026-10-16") and out[2] == "keys: unlocked"
    assert out[3] == "next owner item: %s: The owner switches %s to a project." % (pr.code, pr.code)
    assert len(out) <= 5


def test_awb_alone_says_no_answer_and_none_and_names_the_work_side(home, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    _states(monkeypatch, None, "locked")
    out = cli.status_lines(home)
    assert out[:4] == ["side: work", "vault: no answer", "keys: locked", "next owner item: none"]
